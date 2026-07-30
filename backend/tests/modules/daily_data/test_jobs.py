import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.daily_data.contracts import (
    DailyMissingReason,
    DailyStageStatus,
)
from long_invest.modules.daily_data.jobs import (
    DailyMarketRecoveryJob,
    FullMarketDailyJob,
    _group_stages,
    _groups,
    _historical_absence,
    _plan_snapshot,
    _stored_bar_stage,
)
from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyCollectionMode,
    DailyCollectionPlan,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
)
from long_invest.modules.securities.contracts import FrozenSecurity, ListingStatus
from long_invest.platform.jobs.contracts import JobExecutionContext, JobStatus

DAY = date(2026, 7, 30)
NOW = datetime(2026, 7, 30, 9, tzinfo=UTC)


def _security(symbol: str, *, suspended: bool = False) -> FrozenSecurity:
    return FrozenSecurity(
        security_id=uuid4(),
        symbol=symbol,
        listing_status=ListingStatus.LISTED,
        is_suspended=suspended,
        is_st=False,
        listed_on=date(2020, 1, 1),
        delisted_on=None,
    )


def _bar(symbol: str) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trading_date=DAY,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=100,
        amount=Decimal("1000"),
        source=ProviderCode.EASTMONEY,
        capability=ProviderCapability.DAILY_BAR_UNADJUSTED,
        collected_at=NOW,
    )


@pytest.mark.parametrize(
    ("mode", "group_size", "expected_sizes"),
    [
        (DailyCollectionMode.SNAPSHOT, 150, [150]),
        (DailyCollectionMode.PAGED, 100, [150, 150]),
        (DailyCollectionMode.BATCHED_SYMBOLS, 100, [100, 50]),
        (DailyCollectionMode.SINGLE_SYMBOL, 1, [1] * 150),
    ],
)
def test_collection_plan_builds_recoverable_groups(mode, group_size, expected_sizes):
    symbols = tuple(
        f"{index:06d}.SZ" for index in range(150)
    )
    plan = DailyCollectionPlan(
        ProviderCode.EASTMONEY, mode, len(symbols), group_size, 0.5
    )

    assert [len(group) for group in _groups(plan, symbols)] == expected_sizes


def test_collection_plan_budget_snapshot_is_json_serializable() -> None:
    plan = DailyCollectionPlan(
        ProviderCode.EASTMONEY,
        DailyCollectionMode.SNAPSHOT,
        10,
        10,
        0.5,
    )
    limited_at = NOW.replace(minute=30)

    snapshot = _plan_snapshot(
        plan,
        budget={
            "remaining": 49_000,
            "reset_at": NOW,
            "latest_limited_at": limited_at,
            "capabilities": [],
        },
    )

    assert json.loads(json.dumps(snapshot))["budget"] == {
        "remaining": 49_000,
        "reset_at": NOW.isoformat(),
        "latest_limited_at": limited_at.isoformat(),
        "capabilities": [],
    }


@pytest.mark.anyio
async def test_daily_fallback_honors_cancel_before_single_symbol_requests(
    monkeypatch,
) -> None:
    class Database:
        @asynccontextmanager
        async def session(self):
            yield object()

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def list_stages(self, _batch_id):
            return ()

        async def get_batch(self, _batch_id):
            return SimpleNamespace(requested_count=0)

    monkeypatch.setattr(
        "long_invest.modules.daily_data.jobs.DailyDataRepository",
        Repository,
    )
    job = FullMarketDailyJob(
        Database(),
        provider_service_factory=lambda _session: pytest.fail(
            "canceled fallback must not call the provider"
        ),
    )

    async def report(*_args, **_kwargs):
        return JobStatus.CANCELED

    monkeypatch.setattr(job, "_report", report)
    result = await job._retry_failed(
        JobExecutionContext(
            job_id=uuid4(),
            fence_token=uuid4(),
            config={"trade_date": DAY.isoformat()},
            checkpoint={},
        ),
        uuid4(),
        uuid4(),
        DAY,
        {"600000.SH": _security("600000.SH")},
        DailyCollectionPlan(
            ProviderCode.EASTMONEY,
            DailyCollectionMode.PAGED,
            1,
            100,
            0.5,
        ),
        start_index=0,
    )

    assert result is JobStatus.CANCELED


def test_group_result_isolates_missing_and_suspended_symbols() -> None:
    available = _security("600000.SH")
    suspended = _security("000001.SZ", suspended=True)
    result = ProviderBatchResult(
        (_bar(available.symbol),),
        (
            ProviderItemFailure(
                suspended.symbol,
                "PROVIDER_ITEM_SCHEMA_INVALID",
                "invalid item",
                ProviderCode.EASTMONEY,
            ),
        ),
    )

    stages = _group_stages(
        result,
        expected_symbols=(available.symbol, suspended.symbol),
        security_by_symbol={
            available.symbol: available,
            suspended.symbol: suspended,
        },
        trading_date=DAY,
        now=NOW,
    )

    assert stages[0].status is DailyStageStatus.FETCHED
    assert stages[1].status is DailyStageStatus.MISSING
    assert stages[1].missing_reason is DailyMissingReason.SUSPENDED


def test_group_result_rejects_duplicate_provider_items() -> None:
    security = _security("600000.SH")
    with pytest.raises(ValueError, match="invalid daily market group"):
        _group_stages(
            ProviderBatchResult((_bar(security.symbol), _bar(security.symbol))),
            expected_symbols=(security.symbol,),
            security_by_symbol={security.symbol: security},
            trading_date=DAY,
            now=NOW,
        )


def test_job_rejects_missing_trade_date_before_touching_dependencies() -> None:
    result = asyncio.run(
        FullMarketDailyJob(
            object(), provider_service_factory=lambda _session: object()
        )(
            JobExecutionContext(
                job_id=uuid4(),
                fence_token=uuid4(),
                config={},
                checkpoint={},
            )
        )
    )

    assert result.success is False
    assert result.code == "DAILY_MARKET_CONFIG_INVALID"


def test_recovery_job_rejects_unsorted_dates_before_touching_dependencies() -> None:
    result = asyncio.run(
        DailyMarketRecoveryJob(
            object(), provider_service_factory=lambda _session: object()
        )(
            JobExecutionContext(
                job_id=uuid4(),
                fence_token=uuid4(),
                config={
                    "trade_dates": ["2026-07-16", "2026-07-15"],
                    "concurrency": 4,
                },
                checkpoint={},
            )
        )
    )

    assert result.success is False
    assert result.code == "DAILY_RECOVERY_CONFIG_INVALID"


def test_recovery_reuses_an_existing_bar_without_losing_metadata() -> None:
    security = _security("600000.SH")
    stored = type(
        "StoredBar",
        (),
        {
            "security_id": security.security_id,
            "symbol": security.symbol,
            "trade_date": DAY,
            "open": Decimal("10"),
            "high": Decimal("11"),
            "low": Decimal("9"),
            "close": Decimal("10.5"),
            "previous_close": Decimal("9.8"),
            "volume": 100,
            "amount": Decimal("1000"),
            "source": "EASTMONEY",
            "source_identity": {
                "adapter": "EASTMONEY",
                "upstream": "EASTMONEY",
                "interface": "history",
                "capability": "HISTORICAL_DAILY_UNADJUSTED",
                "algorithm_version": "raw-v1",
            },
            "collected_at": NOW,
        },
    )()

    stage = _stored_bar_stage(stored, security, NOW)

    assert stage.status is DailyStageStatus.FETCHED
    assert stage.provider_payload["previous_close"] == "9.8"
    assert stage.provider_payload["source"] == "EASTMONEY"
    assert stage.provider_payload["source_identity"]["interface"] == "history"


def test_historical_absence_does_not_apply_current_suspension_to_past_dates() -> None:
    security = _security("600000.SH", suspended=True)

    assert _historical_absence(security, DAY) is None
