import asyncio
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.history_backfills.contracts import (
    HistoryBackfillItemError,
    HistoryBarInput,
    HistoryBarsBundle,
    HistoryBarStoreResult,
)
from long_invest.modules.history_backfills.jobs import (
    PostgresHistoryBackfillJob,
)
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobStatus,
)


class Provider:
    def __init__(self, *, failures=None, after_fetch=None, delay=0.0) -> None:
        self.failures = failures or {}
        self.after_fetch = after_fetch
        self.delay = delay
        self.calls = []
        self.concurrencies = []
        self.ranges = []
        self.active = 0
        self.max_active = 0

    async def fetch(self, item, **_values):
        self.calls.append(item.symbol)
        self.concurrencies.append(_values["concurrency"])
        self.ranges.append((_values["start_date"], _values["end_date"]))
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
            return bundle(item.symbol)
        finally:
            self.active -= 1


class Store:
    def __init__(self) -> None:
        self.calls = []

    async def store(self, item, bars, **values):
        self.calls.append((item.symbol, bars, values))
        return HistoryBarStoreResult(
            inserted=1,
            unchanged=0,
            revised=0,
            qfq_dataset_id=uuid4(),
            qfq_version=1,
            qfq_rows=1,
            qfq_actual_start=date(2020, 1, 2),
            qfq_actual_end=date(2020, 1, 2),
            qfq_unchanged=False,
            qfq_truncated_rows=0,
        )


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
        source="SINA",
    )


def bundle(symbol: str) -> HistoryBarsBundle:
    return HistoryBarsBundle(
        unadjusted=(bar(symbol),),
        qfq=(bar(symbol),),
        provider_contract_version="SINA:config-v2",
    )


def context(config=None, checkpoint=None) -> JobExecutionContext:
    return JobExecutionContext(
        job_id=uuid4(),
        fence_token=uuid4(),
        config=MappingProxyType(config or {}),
        checkpoint=MappingProxyType(checkpoint or {}),
    )


class PostgresSubject(PostgresHistoryBackfillJob):
    def __init__(
        self, provider, store, *, status_at=None, disk=None, timeout=1
    ) -> None:
        super().__init__(
            object(),
            provider_factory=lambda: provider,
            store_factory=lambda: store,
            disk_guard_factory=lambda: disk or Disk(),
            item_timeout_seconds=timeout,
        )
        self.reports = []
        self.status_at = status_at

    async def _report(self, _context, **values):
        self.reports.append(values)
        if values.get("pause", False):
            return JobStatus.PAUSED
        if self.status_at is not None and values["cursor"] == self.status_at:
            return JobStatus.PAUSED
        return JobStatus.RUNNING


def postgres_config(snapshot_id, *, concurrency=2):
    return {
        "universe_snapshot_id": str(snapshot_id),
        "start_date": "2010-01-01",
        "end_date": "2020-12-31",
        "concurrency": concurrency,
        "reason": "补齐历史",
    }


def install_frozen_scope(monkeypatch, symbols, *, listed_on=None, delisted_on=None):
    snapshot_id = uuid4()
    items = tuple(
        SimpleNamespace(
            security_id=uuid4(),
            symbol=symbol,
            listed_on=listed_on,
            delisted_on=delisted_on,
        )
        for symbol in symbols
    )

    class Securities:
        def __init__(self, _database):
            pass

        async def frozen_universe(self, actual_snapshot_id):
            assert actual_snapshot_id == snapshot_id
            return SimpleNamespace(items=items)

    monkeypatch.setattr(
        "long_invest.modules.history_backfills.jobs.SecurityApplication",
        Securities,
    )
    return snapshot_id


@pytest.mark.anyio
async def test_postgres_job_uses_internal_cursor_and_configured_concurrency(
    monkeypatch,
) -> None:
    snapshot_id = install_frozen_scope(
        monkeypatch, ("000001.SZ", "600000.SH", "600001.SH")
    )
    provider = Provider(delay=0.01)
    store = Store()
    subject = PostgresSubject(provider, store)

    result = await subject(context(postgres_config(snapshot_id)))

    assert result.success is True
    assert result.data["succeeded"] == 3
    assert provider.max_active == 2
    assert subject.reports[-1]["cursor"] == 3
    assert subject.reports[-1]["counts"]["inserted"] == 3


@pytest.mark.anyio
async def test_postgres_job_resumes_after_the_last_saved_group(monkeypatch) -> None:
    snapshot_id = install_frozen_scope(
        monkeypatch, ("000001.SZ", "600000.SH", "600001.SH")
    )
    first_provider = Provider()
    first = PostgresSubject(first_provider, Store(), status_at=2)

    paused = await first(context(postgres_config(snapshot_id)))
    saved = first.reports[-1]
    checkpoint = {
        "cursor": saved["cursor"],
        "succeeded": saved["succeeded"],
        "failures": saved["failures"],
        "counts": saved["counts"],
    }
    second_provider = Provider()
    resumed = await PostgresSubject(second_provider, Store())(
        context(postgres_config(snapshot_id), checkpoint)
    )

    assert paused.code == "JOB_PAUSED"
    assert first_provider.calls == ["000001.SZ", "600000.SH"]
    assert resumed.success is True
    assert second_provider.calls == ["600001.SH"]


@pytest.mark.anyio
async def test_postgres_job_retry_scope_contains_only_failed_symbols(
    monkeypatch,
) -> None:
    snapshot_id = install_frozen_scope(
        monkeypatch, ("000001.SZ", "600000.SH")
    )
    failed_provider = Provider(
        failures={
            "600000.SH": HistoryBackfillItemError(
                "PROVIDER_TIMEOUT", retryable=True
            )
        }
    )
    first = await PostgresSubject(failed_provider, Store())(
        context(postgres_config(snapshot_id))
    )
    retry_provider = Provider()
    retried = await PostgresSubject(retry_provider, Store())(
        context(
            postgres_config(snapshot_id),
            {"retry_items": first.data["failed_items"]},
        )
    )

    assert first.code == "PARTIAL"
    assert first.data["failed_items"] == ["600000.SH"]
    assert retried.success is True
    assert retry_provider.calls == ["600000.SH"]


@pytest.mark.anyio
async def test_postgres_job_pauses_before_fetch_when_disk_is_full(monkeypatch) -> None:
    snapshot_id = install_frozen_scope(monkeypatch, ("000001.SZ",))
    provider = Provider()
    subject = PostgresSubject(provider, Store(), disk=Disk(False))

    result = await subject(context(postgres_config(snapshot_id)))

    assert result.code == "JOB_PAUSED"
    assert provider.calls == []
    assert subject.reports[-1]["pause"] is True


@pytest.mark.anyio
async def test_complete_mode_uses_dates_frozen_with_the_security_scope(
    monkeypatch,
) -> None:
    snapshot_id = install_frozen_scope(
        monkeypatch,
        ("000001.SZ",),
        listed_on=date(2015, 1, 5),
        delisted_on=date(2020, 12, 31),
    )
    provider = Provider()
    config = postgres_config(snapshot_id)
    config["date_mode"] = "COMPLETE"

    result = await PostgresSubject(provider, Store())(context(config))

    assert result.success is True
    assert provider.ranges == [(date(2015, 1, 5), date(2020, 12, 31))]


@pytest.mark.anyio
async def test_invalid_stock_data_fails_without_blocking_valid_stock(
    monkeypatch,
) -> None:
    snapshot_id = install_frozen_scope(
        monkeypatch, ("000001.SZ", "600000.SH")
    )
    provider = Provider()
    original_fetch = provider.fetch

    async def mixed_fetch(item, **values):
        if item.symbol == "600000.SH":
            return HistoryBarsBundle(
                unadjusted=(bar(item.symbol, high="8"),),
                qfq=(bar(item.symbol),),
                provider_contract_version="SINA:config-v2",
            )
        return await original_fetch(item, **values)

    provider.fetch = mixed_fetch
    store = Store()

    result = await PostgresSubject(provider, store)(
        context(postgres_config(snapshot_id))
    )

    assert result.code == "PARTIAL"
    assert result.data["succeeded"] == 1
    assert result.data["failed_items"] == ["600000.SH"]
    assert [call[0] for call in store.calls] == ["000001.SZ"]


@pytest.mark.anyio
async def test_invalid_day_is_skipped_and_stock_is_marked_anomalous(
    monkeypatch,
) -> None:
    snapshot_id = install_frozen_scope(monkeypatch, ("600000.SH",))
    provider = Provider()

    async def fetch_with_one_bad_day(item, **_values):
        valid = bar(item.symbol)
        invalid = replace(
            valid,
            trade_date=date(2020, 1, 3),
            high=Decimal("8"),
        )
        return HistoryBarsBundle(
            unadjusted=(valid, invalid),
            qfq=(valid, invalid),
            provider_contract_version="SINA:config-v2",
        )

    provider.fetch = fetch_with_one_bad_day
    store = Store()

    result = await PostgresSubject(provider, store)(
        context(postgres_config(snapshot_id))
    )

    assert result.success is True
    assert result.data["failed"] == 0
    assert result.data["anomalous"] == 1
    assert result.data["anomaly_details"][0]["symbol"] == "600000.SH"
    stored_bundle = store.calls[0][1]
    assert [item.trade_date for item in stored_bundle.unadjusted] == [
        date(2020, 1, 2)
    ]
    assert [item.trade_date for item in stored_bundle.qfq] == [date(2020, 1, 2)]


@pytest.mark.anyio
async def test_provider_timeout_is_recorded_as_retryable_stock_failure(
    monkeypatch,
) -> None:
    snapshot_id = install_frozen_scope(monkeypatch, ("600000.SH",))

    result = await PostgresSubject(
        Provider(delay=0.05), Store(), timeout=0.001
    )(context(postgres_config(snapshot_id)))

    assert result.code == "HISTORY_BACKFILL_FAILED"
    assert result.data["failure_details"] == [
        {
            "security_id": result.data["failure_details"][0]["security_id"],
            "symbol": "600000.SH",
            "error_code": "HISTORY_PROVIDER_TIMEOUT",
            "retryable": True,
        }
    ]
