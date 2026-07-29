import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import wraps

import pytest

from long_invest.modules.providers.contracts import (
    CorporateActionRequest,
    DailyBar,
    DailyBarRequest,
    ProviderAdapterCode,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
    ProviderMissingRange,
    ProviderSourceIdentity,
    RealtimeQuote,
    SecurityMasterRecord,
)
from long_invest.modules.providers.resilience import (
    InMemoryProviderRuntimeState,
    ProviderCallError,
    ProviderRoutePlan,
    ProviderRouteSetting,
    StaticProviderConfiguration,
)
from long_invest.modules.providers.router import ProviderRouter


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def quote(symbol: str, source: ProviderCode) -> RealtimeQuote:
    now = datetime.now(UTC)
    return RealtimeQuote(
        symbol,
        Decimal("10"),
        Decimal("10"),
        Decimal("10"),
        Decimal("10"),
        Decimal("10"),
        1,
        Decimal("10"),
        now,
        now,
        source,
    )


def deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=5)


def bar(close: str, source: ProviderCode) -> DailyBar:
    return DailyBar(
        symbol="600000.SH",
        trading_date=date(2025, 1, 2),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal(close),
        volume=100,
        amount=Decimal("1000"),
        source=source,
        capability=ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
    )


class FakeProvider:
    def __init__(self, code: ProviderCode, result: ProviderBatchResult) -> None:
        self.code = code
        self.result = result
        self.quote_requests: list[tuple[str, ...]] = []
        self.bar_requests: list[DailyBarRequest] = []
        self.action_requests: list[CorporateActionRequest] = []
        self.master_requests = 0
        self.master_result: tuple[SecurityMasterRecord, ...] = ()
        self.error: Exception | None = None

    async def realtime_quotes(self, symbols, deadline):
        del deadline
        self.quote_requests.append(symbols)
        if self.error:
            raise self.error
        return self.result

    async def daily_bars(self, request, deadline):
        del deadline
        self.bar_requests.append(request)
        return self.result

    async def corporate_actions(self, request, deadline):
        del deadline
        self.action_requests.append(request)
        return self.result

    async def security_master(self, deadline):
        del deadline
        self.master_requests += 1
        if self.error:
            raise self.error
        return self.master_result


class FixedPlanConfiguration:
    def __init__(self, plan: ProviderRoutePlan) -> None:
        self.plan = plan

    async def route_plan(self, capability):
        assert capability is self.plan.capability
        return self.plan

    async def routes(self, capability):
        return (await self.route_plan(capability)).routes


class RecordingConflicts:
    def __init__(self) -> None:
        self.items = []

    async def record_daily_conflict(self, primary, candidate) -> None:
        self.items.append((primary, candidate))


@async_test
async def test_partial_realtime_result_only_fetches_missing_symbols_from_sina() -> None:
    east = FakeProvider(
        ProviderCode.EASTMONEY,
        ProviderBatchResult(
            (quote("600000.SH", ProviderCode.EASTMONEY),),
            (
                ProviderItemFailure(
                    "000001.SZ",
                    "PROVIDER_ITEM_MISSING",
                    "missing",
                    ProviderCode.EASTMONEY,
                ),
            ),
        ),
    )
    sina = FakeProvider(
        ProviderCode.SINA, ProviderBatchResult((quote("000001.SZ", ProviderCode.SINA),))
    )
    result = await ProviderRouter(east, sina).realtime_quotes(
        ("600000.SH", "000001.SZ"), deadline()
    )
    assert sina.quote_requests == [("000001.SZ",)]
    assert [(item.symbol, item.source) for item in result.items] == [
        ("600000.SH", ProviderCode.EASTMONEY),
        ("000001.SZ", ProviderCode.SINA),
    ]


@async_test
async def test_whole_batch_failure_switches_all_symbols_to_sina() -> None:
    east = FakeProvider(
        ProviderCode.EASTMONEY, ProviderBatchResult(batch_error_code="PROVIDER_FAILED")
    )
    sina = FakeProvider(
        ProviderCode.SINA, ProviderBatchResult((quote("600000.SH", ProviderCode.SINA),))
    )
    result = await ProviderRouter(east, sina).realtime_quotes(
        ("600000.SH",), deadline()
    )
    assert sina.quote_requests == [("600000.SH",)]
    assert result.items[0].source is ProviderCode.SINA


@async_test
async def test_primary_exception_switches_whole_batch_to_sina() -> None:
    east = FakeProvider(ProviderCode.EASTMONEY, ProviderBatchResult())
    east.error = RuntimeError("upstream failed")
    sina = FakeProvider(
        ProviderCode.SINA,
        ProviderBatchResult((quote("600000.SH", ProviderCode.SINA),)),
    )
    result = await ProviderRouter(east, sina).realtime_quotes(
        ("600000.SH",), deadline()
    )
    assert sina.quote_requests == [("600000.SH",)]
    assert result.items[0].source is ProviderCode.SINA


@async_test
async def test_history_uses_sina_only_without_day_level_stitching() -> None:
    east = FakeProvider(
        ProviderCode.EASTMONEY, ProviderBatchResult(batch_error_code="PROVIDER_FAILED")
    )
    sina = FakeProvider(
        ProviderCode.SINA,
        ProviderBatchResult(batch_error_code="PROVIDER_FAILED"),
    )
    request = DailyBarRequest(
        "600000.SH",
        date(2025, 1, 1),
        date(2025, 1, 2),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    result = await ProviderRouter(east, sina).daily_bars(request, deadline())
    assert result.batch_error_code == "PROVIDER_FAILED"
    assert east.bar_requests == []
    assert len(sina.bar_requests) == 1


@async_test
async def test_history_uses_task_selected_concurrency_without_fixed_cap() -> None:
    class CapturingRuntime(InMemoryProviderRuntimeState):
        def __init__(self) -> None:
            super().__init__()
            self.acquired: list[ProviderRouteSetting] = []

        async def acquire(self, setting):
            self.acquired.append(setting)
            return await super().acquire(setting)

    east = FakeProvider(ProviderCode.EASTMONEY, ProviderBatchResult())
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    runtime = CapturingRuntime()
    request = DailyBarRequest(
        "600000.SH",
        date(2025, 1, 1),
        date(2025, 1, 2),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )

    await ProviderRouter(east, sina, runtime=runtime).daily_bars(
        request,
        deadline(),
        concurrency=64,
    )

    assert runtime.acquired[0].concurrency == 64


@async_test
async def test_concurrency_override_is_restricted_to_history() -> None:
    east = FakeProvider(ProviderCode.EASTMONEY, ProviderBatchResult())
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    request = DailyBarRequest(
        "600000.SH",
        date(2025, 1, 1),
        date(2025, 1, 2),
        ProviderCapability.DAILY_BAR_UNADJUSTED,
    )

    with pytest.raises(ValueError, match="historical provider concurrency"):
        await ProviderRouter(east, sina).daily_bars(
            request,
            deadline(),
            concurrency=64,
        )


@async_test
async def test_corporate_actions_use_dedicated_eastmoney_route() -> None:
    east = FakeProvider(ProviderCode.EASTMONEY, ProviderBatchResult())
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    request = CorporateActionRequest("600000.SH", date(2025, 1, 1), date(2025, 12, 31))
    config = StaticProviderConfiguration(
        {
            ProviderCapability.CORPORATE_ACTIONS: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.CORPORATE_ACTIONS,
                ),
            )
        }
    )

    result = await ProviderRouter(east, sina, config=config).corporate_actions(
        request, deadline()
    )

    assert result is east.result
    assert east.action_requests == [request]
    assert sina.action_requests == []


@async_test
async def test_security_master_switches_to_sina_when_eastmoney_fails() -> None:
    east = FakeProvider(ProviderCode.EASTMONEY, ProviderBatchResult())
    east.error = RuntimeError("blocked upstream")
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    sina.master_result = (
        SecurityMasterRecord(
            "600000.SH",
            "浦发银行",
            "SH",
            "A_SHARE",
            None,
            None,
            True,
            False,
            None,
            ProviderCode.SINA,
            datetime.now(UTC),
        ),
    )
    config = StaticProviderConfiguration(
        {
            ProviderCapability.SECURITY_MASTER: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.SECURITY_MASTER,
                    priority=0,
                    auto_switch=True,
                ),
                ProviderRouteSetting(
                    ProviderCode.SINA,
                    ProviderCapability.SECURITY_MASTER,
                    priority=1,
                    auto_switch=False,
                ),
            )
        }
    )

    result = await ProviderRouter(east, sina, config=config).security_master(deadline())

    assert result == sina.master_result
    assert east.master_requests == 1
    assert sina.master_requests == 1


@async_test
async def test_runtime_settings_control_enable_priority_and_auto_switch() -> None:
    east = FakeProvider(
        ProviderCode.EASTMONEY,
        ProviderBatchResult((quote("600000.SH", ProviderCode.EASTMONEY),)),
    )
    sina = FakeProvider(
        ProviderCode.SINA,
        ProviderBatchResult((quote("600000.SH", ProviderCode.SINA),)),
    )
    config = StaticProviderConfiguration(
        {
            ProviderCapability.REALTIME_QUOTE_BATCH: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.REALTIME_QUOTE_BATCH,
                    enabled=False,
                    priority=1,
                ),
                ProviderRouteSetting(
                    ProviderCode.SINA,
                    ProviderCapability.REALTIME_QUOTE_BATCH,
                    enabled=True,
                    priority=2,
                    auto_switch=False,
                ),
            )
        }
    )
    result = await ProviderRouter(east, sina, config=config).realtime_quotes(
        ("600000.SH",), deadline()
    )
    assert east.quote_requests == []
    assert sina.quote_requests == [("600000.SH",)]
    assert result.items[0].source is ProviderCode.SINA


@async_test
async def test_realtime_quotes_from_uses_protected_invocation_pipeline() -> None:
    east = FakeProvider(
        ProviderCode.EASTMONEY,
        ProviderBatchResult((quote("600000.SH", ProviderCode.EASTMONEY),)),
    )
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    config = StaticProviderConfiguration(
        {
            ProviderCapability.REALTIME_QUOTE_BATCH: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.REALTIME_QUOTE_BATCH,
                    enabled=False,
                ),
            )
        }
    )
    router = ProviderRouter(east, sina, config=config)

    with pytest.raises(ProviderCallError) as caught:
        await router.realtime_quotes_from(
            ProviderCode.EASTMONEY,
            ("600000.SH",),
            deadline(),
        )

    assert caught.value.code == "PROVIDER_DISABLED"
    assert east.quote_requests == []


@async_test
async def test_fixed_unavailable_provider_fails_without_silent_switch() -> None:
    east = FakeProvider(ProviderCode.EASTMONEY, ProviderBatchResult())
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    capability = ProviderCapability.REALTIME_QUOTE_BATCH
    config = FixedPlanConfiguration(
        ProviderRoutePlan(capability, (), fixed_provider=ProviderCode.TUSHARE)
    )

    result = await ProviderRouter(east, sina, config=config).realtime_quotes(
        ("600000.SH",), deadline()
    )

    assert result.batch_error_code == "PROVIDER_FIXED_SOURCE_UNAVAILABLE"
    assert east.quote_requests == []
    assert sina.quote_requests == []


@async_test
async def test_unadjusted_history_switches_after_whole_source_failure() -> None:
    east = FakeProvider(
        ProviderCode.EASTMONEY,
        ProviderBatchResult(batch_error_code="PROVIDER_CONNECTION_FAILED"),
    )
    sina = FakeProvider(
        ProviderCode.SINA,
        ProviderBatchResult((bar("10.5", ProviderCode.SINA),)),
    )
    capability = ProviderCapability.HISTORICAL_DAILY_UNADJUSTED
    config = FixedPlanConfiguration(
        ProviderRoutePlan(
            capability,
            (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY, capability, priority=1, auto_switch=True
                ),
                ProviderRouteSetting(
                    ProviderCode.SINA, capability, priority=2, auto_switch=False
                ),
            ),
        )
    )
    request = DailyBarRequest(
        "600000.SH", date(2025, 1, 1), date(2025, 1, 2), capability
    )

    result = await ProviderRouter(east, sina, config=config).daily_bars(
        request, deadline()
    )

    assert result.items == (bar("10.5", ProviderCode.SINA),)
    assert east.bar_requests == [request]
    assert sina.bar_requests == [request]


@async_test
async def test_unadjusted_history_requests_only_reported_missing_range() -> None:
    capability = ProviderCapability.HISTORICAL_DAILY_UNADJUSTED
    missing = ProviderMissingRange("600000.SH", date(2025, 1, 6), date(2025, 1, 8))
    east = FakeProvider(
        ProviderCode.EASTMONEY,
        ProviderBatchResult(
            (bar("10.5", ProviderCode.EASTMONEY),),
            missing_ranges=(missing,),
        ),
    )
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    config = FixedPlanConfiguration(
        ProviderRoutePlan(
            capability,
            (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY, capability, priority=1, auto_switch=True
                ),
                ProviderRouteSetting(
                    ProviderCode.SINA, capability, priority=2, auto_switch=False
                ),
            ),
        )
    )
    request = DailyBarRequest(
        "600000.SH", date(2025, 1, 1), date(2025, 1, 10), capability
    )

    await ProviderRouter(east, sina, config=config).daily_bars(request, deadline())

    assert sina.bar_requests == [
        DailyBarRequest("600000.SH", missing.start, missing.end, capability)
    ]


@async_test
async def test_conflicting_history_rows_are_isolated_for_review() -> None:
    capability = ProviderCapability.HISTORICAL_DAILY_UNADJUSTED
    failure = ProviderItemFailure(
        "600000.SH", "PROVIDER_PARTIAL", "partial", ProviderCode.EASTMONEY
    )
    east = FakeProvider(
        ProviderCode.EASTMONEY,
        ProviderBatchResult((bar("10.5", ProviderCode.EASTMONEY),), (failure,)),
    )
    sina = FakeProvider(
        ProviderCode.SINA,
        ProviderBatchResult((bar("10.8", ProviderCode.SINA),)),
    )
    config = FixedPlanConfiguration(
        ProviderRoutePlan(
            capability,
            (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY, capability, priority=1, auto_switch=True
                ),
                ProviderRouteSetting(
                    ProviderCode.SINA, capability, priority=2, auto_switch=False
                ),
            ),
        )
    )
    request = DailyBarRequest(
        "600000.SH", date(2025, 1, 1), date(2025, 1, 2), capability
    )
    conflicts = RecordingConflicts()

    result = await ProviderRouter(
        east, sina, config=config, conflict_observer=conflicts
    ).daily_bars(request, deadline())

    assert result.items == ()
    assert result.failures[0].code == "PROVIDER_DATA_CONFLICT"
    assert len(conflicts.items) == 1


def test_source_identity_contains_adapter_upstream_interface_and_algorithm() -> None:
    identity = ProviderSourceIdentity(
        adapter=ProviderAdapterCode.AKSHARE,
        upstream=ProviderCode.SINA,
        interface="akshare.stock_zh_a_hist",
        capability=ProviderCapability.HISTORICAL_DAILY_QFQ,
        algorithm_version="akshare-qfq-v1",
    )

    assert identity.contract_version == (
        "AKSHARE:SINA:akshare.stock_zh_a_hist:HISTORICAL_DAILY_QFQ:akshare-qfq-v1"
    )


@async_test
async def test_shared_circuit_and_rate_state_survives_router_recreation() -> None:
    runtime = InMemoryProviderRuntimeState(global_limit=1, realtime_reserved=0)
    east = FakeProvider(
        ProviderCode.EASTMONEY,
        ProviderBatchResult(batch_error_code="PROVIDER_FAILED"),
    )
    sina = FakeProvider(
        ProviderCode.SINA,
        ProviderBatchResult((quote("600000.SH", ProviderCode.SINA),)),
    )
    config = StaticProviderConfiguration(
        {
            ProviderCapability.REALTIME_QUOTE_BATCH: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.REALTIME_QUOTE_BATCH,
                    priority=1,
                    rate_per_second=100,
                ),
                ProviderRouteSetting(
                    ProviderCode.SINA,
                    ProviderCapability.REALTIME_QUOTE_BATCH,
                    priority=2,
                    rate_per_second=100,
                ),
            )
        }
    )
    first = ProviderRouter(east, sina, runtime=runtime, config=config)
    for _ in range(3):
        await first.realtime_quotes(("600000.SH",), deadline())
    east.quote_requests.clear()
    second = ProviderRouter(east, sina, runtime=runtime, config=config)
    result = await second.realtime_quotes(("600000.SH",), deadline())
    assert east.quote_requests == []
    assert result.items[0].source is ProviderCode.SINA
