import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.modules.providers.contracts import ProviderCode, RealtimeQuote
from long_invest.modules.quotes.contracts import (
    CreateQuoteCycle,
    QuoteCycleStatus,
    QuoteItemStatus,
    QuoteSubmission,
)
from long_invest.modules.quotes.service import QuoteCycleService
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 15, 2, 0, tzinfo=UTC)


def symbols(count: int) -> tuple[str, ...]:
    return tuple(f"{600000 + index:06d}.SH" for index in range(count))


def quote(
    symbol: str, price: str = "10.00", source=ProviderCode.EASTMONEY, **overrides
):
    values = {
        "symbol": symbol,
        "price": Decimal(price),
        "open": Decimal("9.90"),
        "high": max(Decimal(price), Decimal("10.10")),
        "low": Decimal("9.80"),
        "previous_close": Decimal("9.95"),
        "volume": 100,
        "amount": Decimal("1000"),
        "quote_time": NOW - timedelta(seconds=10),
        "received_at": NOW,
        "source": source,
    }
    values.update(overrides)
    return RealtimeQuote(**values)


class MemoryRepository:
    def __init__(self):
        self.session = object()
        self.cycles = {}
        self.by_key = {}
        self.lock = asyncio.Lock()
        self.for_update_calls = []

    async def claim_cycle(self, cycle):
        key = (cycle.idempotency_scope, cycle.idempotency_key)
        if key in self.by_key:
            return self.by_key[key], False
        self.cycles[cycle.id] = cycle
        self.by_key[key] = cycle
        return cycle, True

    async def get_with_items(self, cycle_id):
        return self.cycles.get(cycle_id)

    async def get_for_finalize(self, cycle_id):
        await self.lock.acquire()
        return self.cycles.get(cycle_id)

    async def get_for_update(self, cycle_id):
        self.for_update_calls.append(cycle_id)
        await self.lock.acquire()
        return self.cycles.get(cycle_id)

    async def get_item_for_update(self, cycle_id, symbol):
        cycle = self.cycles.get(cycle_id)
        if cycle is None:
            return None
        return next((item for item in cycle.items if item.symbol == symbol), None)

    async def release_finalize(self):
        if self.lock.locked():
            self.lock.release()

    async def list(self, **_kwargs):
        return list(self.cycles.values())

    async def count(self, **_kwargs):
        return len(self.cycles)

    async def list_items(self, cycle_id, **_kwargs):
        cycle = self.cycles.get(cycle_id)
        return list(cycle.items) if cycle else []

    async def find_expired(self, now, limit):
        return [
            c
            for c in self.cycles.values()
            if c.deadline_at
            and c.deadline_at <= now
            and c.status in {QuoteCycleStatus.FETCHING, QuoteCycleStatus.FINALIZING}
        ][:limit]

    async def flush(self):
        return None


class RecordingEvents:
    def __init__(self, session):
        self.session = session
        self.records = []

    async def created(self, cycle):
        self.records.append(("quote_cycle.created", cycle.id))

    async def conflict(self, cycle, item):
        self.records.append(("quote_conflict.detected", item.id))

    async def finalized(self, cycle, valid_items):
        self.records.append(("quote_cycle.finalized", tuple(i.id for i in valid_items)))

    async def missing(self, cycle, abnormal_items):
        self.records.append(
            (
                "quote_item.missing",
                tuple((i.symbol, i.error_code) for i in abnormal_items),
            )
        )


class RecordingQuality:
    def __init__(self):
        self.commands = []

    async def open(self, command):
        self.commands.append(command)


def service(repository=None, events=None, quality=None):
    repository = repository or MemoryRepository()
    events = events or RecordingEvents(repository.session)
    quality = quality or RecordingQuality()
    return (
        QuoteCycleService(repository, events=events, quality_issues=quality),
        repository,
        events,
        quality,
    )


async def create(subject, scope, key="one"):
    return await subject.create(
        CreateQuoteCycle(
            symbols=scope,
            scheduled_at=NOW,
            timeout_seconds=30,
            idempotency_scope="test",
            idempotency_key=key,
            universe_snapshot_id="snapshot-1",
            universe_snapshot_version=7,
        )
    )


@pytest.mark.anyio
async def test_start_uses_cycle_configured_timeout() -> None:
    subject, _, _, _ = service()
    cycle = await subject.create(
        CreateQuoteCycle(
            symbols=symbols(1),
            scheduled_at=NOW,
            timeout_seconds=60,
            idempotency_scope="test",
            idempotency_key="sixty",
            universe_snapshot_id="snapshot-1",
            universe_snapshot_version=7,
        )
    )
    started = await subject.start(cycle.id, NOW)
    assert started.deadline_at == NOW + timedelta(seconds=60)


@pytest.mark.anyio
async def test_create_freezes_subscription_version_separately() -> None:
    subject, repository, _, _ = service()
    command = CreateQuoteCycle(
        symbols=symbols(1),
        scheduled_at=NOW,
        timeout_seconds=30,
        idempotency_scope="test",
        idempotency_key="tracking",
        universe_snapshot_id="universe",
        universe_snapshot_version=7,
        schedule_occurrence_id=uuid4(),
        subscription_snapshot_version=11,
    )
    summary = await subject.create(command)
    stored = repository.cycles[summary.id]
    assert stored.schedule_occurrence_id == command.schedule_occurrence_id
    assert stored.subscription_snapshot_version == 11
    assert stored.items[0].expected_subscription_version == 11


@pytest.mark.anyio
async def test_all_valid_items_finalize_ready_once() -> None:
    subject, _, events, _ = service()
    cycle = await create(subject, symbols(2))
    await subject.start(cycle.id, NOW)
    for symbol in symbols(2):
        await subject.submit(
            cycle.id, QuoteSubmission(symbol, primary=quote(symbol)), NOW
        )
    summary = await subject.finalize(cycle.id, NOW)
    replay = await subject.finalize(cycle.id, NOW)
    assert summary.status == replay.status == QuoteCycleStatus.READY
    assert summary.valid_count == 2
    assert len([e for e in events.records if e[0] == "quote_cycle.finalized"]) == 1


@pytest.mark.anyio
async def test_nineteen_of_twenty_emits_one_aggregate_missing_event() -> None:
    subject, _, events, _ = service()
    cycle = await create(subject, symbols(20))
    await subject.start(cycle.id, NOW)
    for symbol in symbols(19):
        await subject.submit(
            cycle.id, QuoteSubmission(symbol, primary=quote(symbol)), NOW
        )
    summary = await subject.finalize(cycle.id, NOW + timedelta(seconds=31))
    assert summary.status == QuoteCycleStatus.PARTIAL
    assert summary.valid_count == 19
    assert len(summary.eligible_item_ids) == 19
    missing_events = [e for e in events.records if e[0] == "quote_item.missing"]
    assert len(missing_events) == 1 and len(missing_events[0][1]) == 1


@pytest.mark.anyio
async def test_valid_fallback_is_selected_when_primary_is_stale() -> None:
    subject, repository, _, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    stale = quote(symbols(1)[0], quote_time=NOW - timedelta(seconds=181))
    fallback = quote(symbols(1)[0], source=ProviderCode.SINA)
    await subject.submit(cycle.id, QuoteSubmission(symbols(1)[0], stale, fallback), NOW)
    item = repository.cycles[cycle.id].items[0]
    assert item.status == QuoteItemStatus.VALID
    assert item.provider == ProviderCode.SINA


@pytest.mark.anyio
async def test_conflicting_sources_open_quality_issue_and_skip_evaluation() -> None:
    subject, repository, events, quality = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    symbol = symbols(1)[0]
    await subject.submit(
        cycle.id,
        QuoteSubmission(
            symbol, quote(symbol), quote(symbol, "10.10", ProviderCode.SINA)
        ),
        NOW,
    )
    item = repository.cycles[cycle.id].items[0]
    assert item.status == QuoteItemStatus.CONFLICT
    assert item.conflict_evidence["sources"].keys() == {"EASTMONEY", "SINA"}
    assert len(quality.commands) == 1
    assert len([e for e in events.records if e[0] == "quote_conflict.detected"]) == 1


@pytest.mark.anyio
async def test_all_provider_failures_finalize_failed() -> None:
    subject, _, _, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    await subject.submit(
        cycle.id,
        QuoteSubmission(symbols(1)[0], provider_error_code="PROVIDER_DOWN"),
        NOW,
    )
    assert (await subject.finalize(cycle.id, NOW)).status == QuoteCycleStatus.FAILED


@pytest.mark.anyio
async def test_oversized_quote_becomes_invalid_before_database_flush() -> None:
    subject, repository, _, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    symbol = symbols(1)[0]
    oversized = quote(symbol)
    object.__setattr__(oversized, "amount", Decimal("1e20"))
    await subject.submit(cycle.id, QuoteSubmission(symbol, primary=oversized), NOW)
    item = repository.cycles[cycle.id].items[0]
    assert item.status == QuoteItemStatus.INVALID
    assert item.error_code == "QUOTE_QUANTITY_INVALID"


@pytest.mark.anyio
async def test_late_and_duplicate_submissions_do_not_change_terminal_fact() -> None:
    subject, repository, events, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    await subject.finalize(cycle.id, NOW + timedelta(seconds=31))
    before = list(events.records)
    await subject.submit(
        cycle.id,
        QuoteSubmission(symbols(1)[0], primary=quote(symbols(1)[0])),
        NOW + timedelta(seconds=32),
    )
    await subject.submit(
        cycle.id,
        QuoteSubmission(symbols(1)[0], provider_error_code="LATE"),
        NOW + timedelta(seconds=33),
    )
    item = repository.cycles[cycle.id].items[0]
    assert item.status == QuoteItemStatus.TIMEOUT
    assert events.records == before


@pytest.mark.anyio
async def test_submission_after_deadline_is_rejected_before_item_changes() -> None:
    subject, repository, _, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    with pytest.raises(AppError) as caught:
        await subject.submit(
            cycle.id,
            QuoteSubmission(symbols(1)[0], primary=quote(symbols(1)[0])),
            NOW + timedelta(seconds=31),
        )
    assert caught.value.code == "QUOTE_CYCLE_DEADLINE_EXCEEDED"
    assert repository.cycles[cycle.id].items[0].error_code is None
    summary = await subject.finalize(cycle.id, NOW + timedelta(seconds=31))
    assert summary.status == QuoteCycleStatus.FAILED
    assert repository.cycles[cycle.id].items[0].status == QuoteItemStatus.TIMEOUT


@pytest.mark.anyio
async def test_duplicate_submission_does_not_replace_first_valid_quote() -> None:
    subject, repository, _, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    symbol = symbols(1)[0]
    await subject.submit(
        cycle.id, QuoteSubmission(symbol, primary=quote(symbol, "10.00")), NOW
    )
    await subject.submit(
        cycle.id, QuoteSubmission(symbol, primary=quote(symbol, "10.05")), NOW
    )
    assert repository.cycles[cycle.id].items[0].price == Decimal("10.00")


@pytest.mark.anyio
async def test_duplicate_creation_replays_same_cycle_without_event() -> None:
    subject, _, events, _ = service()
    first = await create(subject, symbols(1))
    replay = await create(subject, symbols(1))
    assert replay.id == first.id
    assert [e[0] for e in events.records].count("quote_cycle.created") == 1


@pytest.mark.anyio
async def test_concurrent_finalize_publishes_once() -> None:
    subject, _, events, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    await subject.submit(
        cycle.id, QuoteSubmission(symbols(1)[0], primary=quote(symbols(1)[0])), NOW
    )
    results = await asyncio.wait_for(
        asyncio.gather(
            subject.finalize(cycle.id, NOW), subject.finalize(cycle.id, NOW)
        ),
        2,
    )
    assert results[0].status == results[1].status == QuoteCycleStatus.READY
    assert [e[0] for e in events.records].count("quote_cycle.finalized") == 1


@pytest.mark.anyio
async def test_recover_expired_finalizes_only_expired_active_cycles() -> None:
    subject, repository, _, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    recovered = await subject.recover_expired(NOW + timedelta(seconds=31))
    assert recovered == (cycle.id,)
    assert repository.cycles[cycle.id].status == QuoteCycleStatus.FAILED


@pytest.mark.anyio
async def test_not_expected_to_trade_does_not_degrade_ready_cycle() -> None:
    subject, _, _, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    await subject.submit(
        cycle.id, QuoteSubmission(symbols(1)[0], not_expected_to_trade=True), NOW
    )
    summary = await subject.finalize(cycle.id, NOW)
    assert summary.status == QuoteCycleStatus.READY
    assert summary.valid_count == 0


@pytest.mark.anyio
async def test_event_failure_is_propagated_without_committing() -> None:
    repository = MemoryRepository()
    events = RecordingEvents(repository.session)

    async def fail(_cycle):
        raise RuntimeError("outbox failed")

    events.created = fail
    subject, _, _, _ = service(repository, events)
    with pytest.raises(RuntimeError, match="outbox failed"):
        await create(subject, symbols(1))


@pytest.mark.anyio
async def test_mark_missed_locks_and_only_accepts_unstarted_overdue_pending() -> None:
    subject, repository, _, _ = service()
    cycle = await create(subject, symbols(1))
    with pytest.raises(AppError) as early:
        await subject.mark_missed(cycle.id, NOW)
    assert early.value.code == "QUOTE_CYCLE_STATE_CONFLICT"
    result = await subject.mark_missed(cycle.id, NOW + timedelta(microseconds=1))
    assert result.status == QuoteCycleStatus.MISSED
    assert repository.for_update_calls == [cycle.id, cycle.id]


@pytest.mark.anyio
async def test_mark_missed_cannot_overwrite_fetching_or_ready_cycle() -> None:
    subject, repository, events, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    with pytest.raises(AppError) as fetching:
        await subject.mark_missed(cycle.id, NOW + timedelta(seconds=1))
    assert fetching.value.code == "QUOTE_CYCLE_STATE_CONFLICT"
    symbol = symbols(1)[0]
    await subject.submit(cycle.id, QuoteSubmission(symbol, primary=quote(symbol)), NOW)
    ready = await subject.finalize(cycle.id, NOW)
    replay = await subject.mark_missed(cycle.id, NOW + timedelta(seconds=2))
    assert ready.status == replay.status == QuoteCycleStatus.READY
    assert repository.cycles[cycle.id].status == QuoteCycleStatus.READY
    assert [event[0] for event in events.records].count("quote_cycle.finalized") == 1


@pytest.mark.anyio
async def test_cancel_locks_and_rejects_non_cancelable_state() -> None:
    subject, repository, _, _ = service()
    cycle = await create(subject, symbols(1))
    canceled = await subject.cancel(cycle.id, NOW, "operator")
    replay = await subject.cancel(cycle.id, NOW, "other")
    assert canceled.status == replay.status == QuoteCycleStatus.CANCELED
    assert repository.cycles[cycle.id].cancel_reason == "operator"
    assert repository.for_update_calls == [cycle.id, cycle.id]


@pytest.mark.anyio
async def test_submit_rejects_symbol_outside_frozen_scope() -> None:
    subject, _, _, _ = service()
    cycle = await create(subject, symbols(1))
    await subject.start(cycle.id, NOW)
    with pytest.raises(AppError) as caught:
        await subject.submit(
            cycle.id, QuoteSubmission("000001.SZ", primary=quote("000001.SZ")), NOW
        )
    assert caught.value.code == "QUOTE_ITEM_NOT_IN_SCOPE"
