import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import wraps

import pytest

from long_invest.modules.providers.contracts import (
    CorporateActionRequest,
    DailyBarRequest,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
    RealtimeQuote,
)
from long_invest.modules.providers.resilience import (
    InMemoryProviderRuntimeState,
    ProviderCallError,
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


class FakeProvider:
    def __init__(self, code: ProviderCode, result: ProviderBatchResult) -> None:
        self.code = code
        self.result = result
        self.quote_requests: list[tuple[str, ...]] = []
        self.bar_requests: list[DailyBarRequest] = []
        self.action_requests: list[CorporateActionRequest] = []
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
async def test_history_uses_eastmoney_only_without_day_level_stitching() -> None:
    east = FakeProvider(
        ProviderCode.EASTMONEY, ProviderBatchResult(batch_error_code="PROVIDER_FAILED")
    )
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    request = DailyBarRequest(
        "600000.SH",
        date(2025, 1, 1),
        date(2025, 1, 2),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    result = await ProviderRouter(east, sina).daily_bars(request, deadline())
    assert result.batch_error_code == "PROVIDER_FAILED"
    assert len(east.bar_requests) == 1
    assert sina.bar_requests == []


@async_test
async def test_corporate_actions_use_dedicated_eastmoney_route() -> None:
    east = FakeProvider(ProviderCode.EASTMONEY, ProviderBatchResult())
    sina = FakeProvider(ProviderCode.SINA, ProviderBatchResult())
    request = CorporateActionRequest(
        "600000.SH", date(2025, 1, 1), date(2025, 12, 31)
    )
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
