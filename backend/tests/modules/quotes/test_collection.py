import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import long_invest.modules.quotes.collection as collection_module
from long_invest.modules.providers.contracts import (
    ProviderBatchResult,
    ProviderCode,
    ProviderItemFailure,
    RealtimeQuote,
)
from long_invest.modules.quotes.collection import QuoteCollectionService
from long_invest.modules.quotes.contracts import (
    CreateQuoteCycle,
    QuoteCycleStatus,
    QuoteCycleSummary,
)

NOW = datetime(2026, 7, 16, 2, 0, tzinfo=UTC)


def quote(symbol: str, source: ProviderCode = ProviderCode.EASTMONEY):
    return RealtimeQuote(
        symbol=symbol,
        price=Decimal("10"),
        open=Decimal("9.9"),
        high=Decimal("10.1"),
        low=Decimal("9.8"),
        previous_close=Decimal("9.95"),
        volume=100,
        amount=Decimal("1000"),
        quote_time=NOW - timedelta(seconds=10),
        received_at=NOW,
        source=source,
    )


class Provider:
    def __init__(self, primary, fallback, after_primary=None):
        self.primary = primary
        self.fallback = fallback
        self.after_primary = after_primary
        self.calls = []

    async def realtime_quotes_from(self, provider, symbols, deadline):
        self.calls.append((provider, symbols, deadline))
        if provider is ProviderCode.EASTMONEY:
            if self.after_primary is not None:
                self.after_primary()
            return self.primary
        return self.fallback


class BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def realtime_quotes_from(self, provider, symbols, deadline):
        self.started.set()
        await asyncio.Event().wait()


class Cycles:
    def __init__(
        self,
        final_status=QuoteCycleStatus.READY,
        deadline_at=NOW + timedelta(seconds=30),
        start_status=QuoteCycleStatus.FETCHING,
        start_gate=None,
        cancel_gate=None,
        cancel_error=None,
    ):
        self.cycle_id = uuid4()
        self.final_status = final_status
        self.deadline_at = deadline_at
        self.start_status = start_status
        self.start_gate = start_gate
        self.cancel_gate = cancel_gate
        self.cancel_error = cancel_error
        self.start_committed = asyncio.Event()
        self.start_cancelled = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.cancel_cancelled = asyncio.Event()
        self.created = []
        self.submissions = []
        self.finalized = []
        self.canceled = []
        self.expected_count = 0

    async def create_and_start(self, command, now):
        self.created.append((command, now))
        self.expected_count = len(command.symbols)
        self.start_committed.set()
        if self.start_gate is not None:
            try:
                await self.start_gate.wait()
            except asyncio.CancelledError:
                self.start_cancelled.set()
                raise
        return self._summary(self.start_status)

    async def submit(self, cycle_id, submission, now):
        self.submissions.append((cycle_id, submission, now))

    async def finalize(self, cycle_id, now):
        if len(self.submissions) < self.expected_count and now < self.deadline_at:
            raise RuntimeError("QUOTE_CYCLE_STATE_CONFLICT")
        self.finalized.append((cycle_id, now))
        return self._summary(self.final_status)

    async def cancel(self, cycle_id, now, reason):
        self.cancel_started.set()
        if self.cancel_gate is not None:
            try:
                await self.cancel_gate.wait()
            except asyncio.CancelledError:
                self.cancel_cancelled.set()
                raise
        if self.cancel_error is not None:
            raise self.cancel_error
        self.canceled.append((cycle_id, now, reason))
        return self._summary(QuoteCycleStatus.CANCELED)

    def _summary(self, status):
        return QuoteCycleSummary(
            id=self.cycle_id,
            status=status,
            expected_count=self.expected_count,
            valid_count=len(self.submissions),
            missing_count=0,
            conflict_count=0,
            failed_count=0,
            eligible_item_ids=(),
            eligible_symbols=(),
            scheduled_at=NOW,
            started_at=NOW,
            deadline_at=self.deadline_at,
            finalized_at=NOW,
            schedule_occurrence_id=None,
            subscription_snapshot_version=None,
        )

def command():
    return CreateQuoteCycle(
        symbols=("600000.SH", "000001.SZ"),
        scheduled_at=NOW,
        timeout_seconds=30,
        idempotency_scope="quotes:manual:user-1",
        idempotency_key="key-1",
        universe_snapshot_id=str(uuid4()),
        universe_snapshot_version=7,
    )


@pytest.mark.anyio
async def test_collection_only_falls_back_for_missing_or_invalid_primary() -> None:
    stale = replace(
        quote("000001.SZ"), quote_time=NOW - timedelta(seconds=181)
    )
    fallback = quote("000001.SZ", ProviderCode.SINA)
    provider = Provider(
        ProviderBatchResult(items=(quote("600000.SH"), stale)),
        ProviderBatchResult(items=(fallback,)),
    )
    cycles = Cycles()

    result = await QuoteCollectionService(provider, cycles, now=lambda: NOW).collect(
        command()
    )

    assert result.status is QuoteCycleStatus.READY
    assert [call[1] for call in provider.calls] == [
        ("600000.SH", "000001.SZ"),
        ("000001.SZ",),
    ]
    by_symbol = {item[1].symbol: item[1] for item in cycles.submissions}
    assert by_symbol["600000.SH"].fallback is None
    assert by_symbol["000001.SZ"].primary is stale
    assert by_symbol["000001.SZ"].fallback is fallback


@pytest.mark.anyio
async def test_collection_isolates_provider_failure_and_finalizes_batch() -> None:
    failure = ProviderItemFailure(
        symbol="000001.SZ",
        code="PROVIDER_HTTP_TIMEOUT",
        message="timeout",
        provider=ProviderCode.SINA,
    )
    provider = Provider(
        ProviderBatchResult(items=(quote("600000.SH"),)),
        ProviderBatchResult(failures=(failure,)),
    )
    cycles = Cycles(final_status=QuoteCycleStatus.PARTIAL)

    result = await QuoteCollectionService(provider, cycles, now=lambda: NOW).collect(
        command()
    )

    assert result.status is QuoteCycleStatus.PARTIAL
    missing = next(
        item[1] for item in cycles.submissions if item[1].symbol == "000001.SZ"
    )
    assert missing.provider_error_code == "PROVIDER_HTTP_TIMEOUT"
    assert cycles.finalized == [(cycles.cycle_id, NOW)]


@pytest.mark.anyio
async def test_late_primary_response_is_not_saved_or_sent_to_fallback() -> None:
    clock = {"now": NOW}
    provider = Provider(
        ProviderBatchResult(items=(quote("600000.SH"),)),
        ProviderBatchResult(items=(quote("000001.SZ", ProviderCode.SINA),)),
        after_primary=lambda: clock.update(now=NOW + timedelta(seconds=31)),
    )
    cycles = Cycles(final_status=QuoteCycleStatus.FAILED)

    await QuoteCollectionService(
        provider, cycles, now=lambda: clock["now"]
    ).collect(command())

    assert [call[0] for call in provider.calls] == [ProviderCode.EASTMONEY]
    assert cycles.submissions == []
    assert cycles.finalized == [(cycles.cycle_id, NOW + timedelta(seconds=31))]


@pytest.mark.anyio
async def test_recovered_expired_cycle_finalizes_without_calling_provider() -> None:
    provider = Provider(ProviderBatchResult(), ProviderBatchResult())
    cycles = Cycles(
        final_status=QuoteCycleStatus.FAILED,
        deadline_at=NOW - timedelta(seconds=1),
    )

    result = await QuoteCollectionService(
        provider, cycles, now=lambda: NOW
    ).collect(command())

    assert result.status is QuoteCycleStatus.FAILED
    assert provider.calls == []
    assert cycles.finalized == [(cycles.cycle_id, NOW)]


@pytest.mark.anyio
async def test_collection_uses_persisted_cycle_deadline_for_provider_calls() -> None:
    deadline = NOW + timedelta(seconds=5)
    provider = Provider(
        ProviderBatchResult(items=(quote("600000.SH"), quote("000001.SZ"))),
        ProviderBatchResult(),
    )
    cycles = Cycles(deadline_at=deadline)

    await QuoteCollectionService(provider, cycles, now=lambda: NOW).collect(command())

    assert [call[2] for call in provider.calls] == [deadline]


@pytest.mark.anyio
async def test_collection_cancels_cycle_when_provider_fetch_is_cancelled() -> None:
    provider = BlockingProvider()
    cycles = Cycles(final_status=QuoteCycleStatus.FAILED)
    task = asyncio.create_task(
        QuoteCollectionService(provider, cycles, now=lambda: NOW).collect(command())
    )
    await provider.started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cycles.canceled == [
        (cycles.cycle_id, NOW, "JOB_EXECUTION_CANCELED")
    ]
    assert cycles.finalized == []


@pytest.mark.anyio
async def test_cancellation_during_start_waits_for_cycle_then_cancels_it() -> None:
    start_gate = asyncio.Event()
    provider = Provider(ProviderBatchResult(), ProviderBatchResult())
    cycles = Cycles(start_gate=start_gate)
    task = asyncio.create_task(
        QuoteCollectionService(provider, cycles, now=lambda: NOW).collect(command())
    )
    await cycles.start_committed.wait()

    task.cancel()
    start_gate.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cycles.canceled == [
        (cycles.cycle_id, NOW, "JOB_EXECUTION_CANCELED")
    ]
    assert provider.calls == []


@pytest.mark.anyio
async def test_cancellation_during_stuck_start_is_bounded_and_reaped(
    monkeypatch,
) -> None:
    start_gate = asyncio.Event()
    provider = Provider(ProviderBatchResult(), ProviderBatchResult())
    cycles = Cycles(start_gate=start_gate)
    logged = []
    loop_errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    class Logger:
        def error(self, event, **values):
            logged.append((event, values))

    monkeypatch.setattr(collection_module, "logger", Logger(), raising=False)
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        task = asyncio.create_task(
            QuoteCollectionService(
                provider,
                cycles,
                now=lambda: NOW,
                cleanup_timeout_seconds=0.01,
            ).collect(command())
        )
        await cycles.start_committed.wait()

        task.cancel()

        async with asyncio.timeout(0.1):
            with pytest.raises(asyncio.CancelledError):
                await task
        await cycles.start_cancelled.wait()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert logged == [
        (
            "quote_cycle_cancellation_cleanup_timed_out",
            {
                "category": "worker",
                "phase": "create_and_start",
                "cycle_id": None,
                "timeout_seconds": 0.01,
            },
        )
    ]
    assert loop_errors == []
    assert provider.calls == []


@pytest.mark.anyio
async def test_second_cancellation_during_stuck_cycle_cancel_is_bounded_and_reaped(
    monkeypatch,
) -> None:
    cancel_gate = asyncio.Event()
    provider = BlockingProvider()
    cycles = Cycles(cancel_gate=cancel_gate)
    logged = []
    loop_errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    class Logger:
        def error(self, event, **values):
            logged.append((event, values))

    monkeypatch.setattr(collection_module, "logger", Logger(), raising=False)
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    task = asyncio.create_task(
        QuoteCollectionService(
            provider,
            cycles,
            now=lambda: NOW,
            cleanup_timeout_seconds=0.01,
        ).collect(command())
    )
    try:
        await provider.started.wait()

        task.cancel()
        await cycles.cancel_started.wait()
        task.cancel()

        async with asyncio.timeout(0.1):
            with pytest.raises(asyncio.CancelledError):
                await task
        await cycles.cancel_cancelled.wait()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert logged == [
        (
            "quote_cycle_cancellation_cleanup_timed_out",
            {
                "category": "worker",
                "phase": "cancel_cycle",
                "cycle_id": str(cycles.cycle_id),
                "timeout_seconds": 0.01,
            },
        )
    ]
    assert loop_errors == []
    assert cycles.canceled == []


@pytest.mark.anyio
async def test_cancel_cleanup_failure_is_logged_without_replacing_cancellation(
    monkeypatch,
) -> None:
    provider = BlockingProvider()
    cycles = Cycles(cancel_error=RuntimeError("database unavailable"))
    logged = []

    class Logger:
        def exception(self, event, **values):
            logged.append((event, values))

    monkeypatch.setattr(collection_module, "logger", Logger(), raising=False)
    task = asyncio.create_task(
        QuoteCollectionService(provider, cycles, now=lambda: NOW).collect(command())
    )
    await provider.started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert logged == [
        (
            "quote_cycle_cancellation_cleanup_failed",
            {
                "category": "worker",
                "phase": "cancel_cycle",
                "cycle_id": str(cycles.cycle_id),
                "error_type": "RuntimeError",
            },
        )
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        QuoteCycleStatus.READY,
        QuoteCycleStatus.PARTIAL,
        QuoteCycleStatus.FAILED,
        QuoteCycleStatus.MISSED,
        QuoteCycleStatus.CANCELED,
    ],
)
async def test_recovered_terminal_cycle_returns_without_provider_call(status) -> None:
    provider = Provider(ProviderBatchResult(), ProviderBatchResult())
    cycles = Cycles(start_status=status)

    result = await QuoteCollectionService(
        provider, cycles, now=lambda: NOW
    ).collect(command())

    assert result.status is status
    assert provider.calls == []
    assert cycles.finalized == []
    assert cycles.canceled == []
