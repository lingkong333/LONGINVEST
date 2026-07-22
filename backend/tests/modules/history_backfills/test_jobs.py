import asyncio
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from uuid import uuid4

import pytest

from long_invest.modules.history_backfills.contracts import (
    HistoryBackfillControl,
    HistoryBackfillItemError,
    HistoryBackfillWorkItem,
    HistoryBarInput,
    HistoryBarStoreResult,
    HistoryJobItemSummary,
)
from long_invest.modules.history_backfills.jobs import (
    HistoryBackfillExecutor,
    build_history_backfill_handler,
)
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobItemStatus,
)


class Items:
    def __init__(self, symbols, *, succeeded=(), active=()) -> None:
        self.job_control = HistoryBackfillControl.RUNNING
        self.statuses = {
            symbol: (
                JobItemStatus.SUCCEEDED
                if symbol in succeeded
                else JobItemStatus.FETCHING
                if symbol in active
                else JobItemStatus.PENDING
            )
            for symbol in symbols
        }
        self.security_ids = {symbol: uuid4() for symbol in symbols}
        self.results = {}
        self.errors = {}
        self.pause_reasons = []
        self.claim_limits = []
        self.recovered = 0
        self.progress_reports = []

    async def recover_incomplete(self, _fence):
        self.recovered += 1
        for symbol, status in self.statuses.items():
            if status in {
                JobItemStatus.FETCHING,
                JobItemStatus.VALIDATING,
                JobItemStatus.RUNNING,
                JobItemStatus.SAVING,
            }:
                self.statuses[symbol] = JobItemStatus.PENDING

    async def control(self, _job_id):
        return self.job_control

    async def claim_pending(self, _job_id, *, limit):
        self.claim_limits.append(limit)
        symbols = [
            symbol
            for symbol, status in self.statuses.items()
            if status is JobItemStatus.PENDING
        ][:limit]
        for symbol in symbols:
            self.statuses[symbol] = JobItemStatus.FETCHING
        return tuple(
            HistoryBackfillWorkItem(self.security_ids[symbol], symbol)
            for symbol in symbols
        )

    async def mark_stage(self, _job_id, symbol, status):
        self.statuses[symbol] = status

    async def release_pending(self, _job_id, symbol):
        self.statuses[symbol] = JobItemStatus.PENDING

    async def finish(
        self,
        _job_id,
        symbol,
        *,
        status,
        result_ref=None,
        error_code=None,
    ):
        self.statuses[symbol] = status
        self.results[symbol] = result_ref
        self.errors[symbol] = error_code

    async def summary(self, _job_id):
        active_statuses = {
            JobItemStatus.FETCHING,
            JobItemStatus.VALIDATING,
            JobItemStatus.RUNNING,
            JobItemStatus.SAVING,
        }
        values = tuple(self.statuses.values())
        return HistoryJobItemSummary(
            total=len(values),
            pending=values.count(JobItemStatus.PENDING),
            active=sum(status in active_statuses for status in values),
            succeeded=values.count(JobItemStatus.SUCCEEDED),
            failed=values.count(JobItemStatus.FAILED),
            canceled=values.count(JobItemStatus.CANCELED),
        )

    async def request_pause(self, _job_id, *, reason):
        self.pause_reasons.append(reason)
        self.job_control = HistoryBackfillControl.PAUSE_REQUESTED

    async def report_progress(self, _fence, summary):
        self.progress_reports.append(summary)

    async def cancel_pending(self, _fence):
        for symbol, status in self.statuses.items():
            if status is JobItemStatus.PENDING:
                self.statuses[symbol] = JobItemStatus.CANCELED


class Provider:
    def __init__(self, *, failures=None, after_fetch=None, delay=0.0) -> None:
        self.failures = failures or {}
        self.after_fetch = after_fetch
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def fetch(self, item, **_values):
        self.calls.append(item.symbol)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            failure = self.failures.get(item.symbol)
            if failure:
                raise failure
            if self.after_fetch:
                self.after_fetch(item.symbol)
            return (bar(item.symbol),)
        finally:
            self.active -= 1


class Store:
    def __init__(self) -> None:
        self.calls = []

    async def store(self, item, bars, **values):
        self.calls.append((item.symbol, bars, values))
        return HistoryBarStoreResult(inserted=1, unchanged=0, revised=0)


class Disk:
    def __init__(self, safe=True) -> None:
        self.safe = safe

    async def is_backfill_safe(self):
        return self.safe


def bar(symbol, *, high="11", low="9") -> HistoryBarInput:
    return HistoryBarInput(
        symbol=symbol,
        trade_date=date(2020, 1, 2),
        open=Decimal("10"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("10.5"),
        volume=100,
        amount=Decimal("1000"),
        source="EASTMONEY",
    )


def context(config=None) -> JobExecutionContext:
    return JobExecutionContext(
        job_id=uuid4(),
        fence_token=uuid4(),
        config=MappingProxyType(config or {}),
    )


def executor(provider, items, store=None, disk=None, timeout=1):
    return HistoryBackfillExecutor(
        provider=provider,
        store=store or Store(),
        items=items,
        disk_guard=disk or Disk(),
        item_timeout_seconds=timeout,
    )


@pytest.mark.anyio
async def test_executor_limits_concurrency_and_completes_each_item() -> None:
    items = Items(("000001.SZ", "600000.SH", "600001.SH"))
    provider = Provider(delay=0.01)
    store = Store()
    job_context = context()
    result = await executor(provider, items, store=store).execute(
        job_context,
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=2,
        reason="补齐历史",
    )
    assert result.succeeded == 3
    assert result.failed == 0
    assert provider.max_active == 2
    assert items.claim_limits == [2, 2, 2]
    assert items.recovered == 1
    assert items.progress_reports[-1].succeeded == 3
    assert all(
        values["idempotency_key"].startswith(f"history:{job_context.job_id}:")
        for _symbol, _bars, values in store.calls
    )


@pytest.mark.anyio
async def test_one_provider_failure_does_not_block_other_stocks() -> None:
    items = Items(("000001.SZ", "600000.SH"))
    provider = Provider(
        failures={
            "000001.SZ": HistoryBackfillItemError(
                "PROVIDER_CIRCUIT_OPEN", retryable=True
            )
        }
    )
    result = await executor(provider, items).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=2,
        reason="补齐历史",
    )
    assert result.succeeded == 1
    assert result.failed == 1
    assert items.errors["000001.SZ"] == "PROVIDER_CIRCUIT_OPEN"


@pytest.mark.anyio
async def test_pause_after_fetch_releases_item_without_writing() -> None:
    items = Items(("600000.SH",))
    provider = Provider(
        after_fetch=lambda _symbol: setattr(
            items, "job_control", HistoryBackfillControl.PAUSE_REQUESTED
        )
    )
    store = Store()
    result = await executor(provider, items, store=store).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=1,
        reason="补齐历史",
    )
    assert result.control is HistoryBackfillControl.PAUSE_REQUESTED
    assert items.statuses["600000.SH"] is JobItemStatus.PENDING
    assert store.calls == []


@pytest.mark.anyio
async def test_cancel_after_fetch_marks_item_canceled() -> None:
    items = Items(("600000.SH",))
    provider = Provider(
        after_fetch=lambda _symbol: setattr(
            items, "job_control", HistoryBackfillControl.CANCEL_REQUESTED
        )
    )
    result = await executor(provider, items).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=1,
        reason="补齐历史",
    )
    assert result.canceled == 1
    assert items.statuses["600000.SH"] is JobItemStatus.CANCELED


@pytest.mark.anyio
async def test_cancel_before_claim_marks_all_pending_items_canceled() -> None:
    items = Items(("000001.SZ", "600000.SH"))
    items.job_control = HistoryBackfillControl.CANCEL_REQUESTED
    provider = Provider()
    result = await executor(provider, items).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=2,
        reason="取消回填",
    )
    assert result.canceled == 2
    assert provider.calls == []


@pytest.mark.anyio
async def test_disk_guard_requests_pause_before_claiming() -> None:
    items = Items(("600000.SH",))
    provider = Provider()
    result = await executor(provider, items, disk=Disk(False)).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=1,
        reason="补齐历史",
    )
    assert result.control is HistoryBackfillControl.PAUSE_REQUESTED
    assert provider.calls == []
    assert items.pause_reasons == ["HISTORY_DISK_CAPACITY_LOW"]


@pytest.mark.anyio
async def test_successful_item_is_not_fetched_on_resume_or_retry() -> None:
    items = Items(
        ("000001.SZ", "600000.SH"),
        succeeded=("000001.SZ",),
    )
    provider = Provider()
    result = await executor(provider, items).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=2,
        reason="重试失败项",
    )
    assert result.succeeded == 2
    assert provider.calls == ["600000.SH"]


@pytest.mark.anyio
async def test_recovery_requeues_only_incomplete_item() -> None:
    items = Items(
        ("000001.SZ", "600000.SH"),
        succeeded=("000001.SZ",),
        active=("600000.SH",),
    )
    provider = Provider()
    result = await executor(provider, items).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=1,
        reason="中断恢复",
    )
    assert result.succeeded == 2
    assert provider.calls == ["600000.SH"]


@pytest.mark.anyio
async def test_invalid_bar_fails_before_store() -> None:
    items = Items(("600000.SH",))
    provider = Provider()

    async def invalid_fetch(_item, **_values):
        return (bar("600000.SH", high="8"),)

    provider.fetch = invalid_fetch
    store = Store()
    await executor(provider, items, store=store).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=1,
        reason="补齐历史",
    )
    assert items.errors["600000.SH"] == "HISTORY_BARS_INVALID"
    assert store.calls == []


@pytest.mark.anyio
async def test_provider_timeout_isolated_to_item() -> None:
    items = Items(("600000.SH",))
    await executor(Provider(delay=0.05), items, timeout=0.001).execute(
        context(),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=1,
        reason="补齐历史",
    )
    assert items.errors["600000.SH"] == "HISTORY_PROVIDER_TIMEOUT"


@pytest.mark.anyio
async def test_handler_rejects_invalid_frozen_config() -> None:
    handler = build_history_backfill_handler(
        provider_factory=Provider,
        store_factory=Store,
        items_factory=lambda: Items(()),
        disk_guard_factory=Disk,
    )
    result = await handler(context({}))
    assert result.success is False
    assert result.code == "HISTORY_BACKFILL_CONFIG_INVALID"
