import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from long_invest.modules.providers.baostock import BaoStockProvider
from long_invest.modules.providers.contracts import (
    DailyCollectionMode,
    DailyCollectionPlan,
    MarketDailyGroupRequest,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    RealtimeQuote,
)
from long_invest.modules.providers.eastmoney import EastmoneyProvider
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.modules.providers.sina import SinaRealtimeProvider
from long_invest.modules.providers.tushare import TushareProvider


def deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=5)


def test_adapters_declare_all_supported_daily_collection_modes() -> None:
    async def token() -> str:
        return "test"

    plans = (
        EastmoneyProvider(None).daily_collection_plan(5500),
        SinaRealtimeProvider(None).daily_collection_plan(5500),
        TushareProvider(token_resolver=token).daily_collection_plan(5500),
        BaoStockProvider().daily_collection_plan(5500),
    )
    assert [item.mode for item in plans] == [
        DailyCollectionMode.PAGED,
        DailyCollectionMode.BATCHED_SYMBOLS,
        DailyCollectionMode.SNAPSHOT,
        DailyCollectionMode.SINGLE_SYMBOL,
    ]
    assert [item.estimated_requests for item in plans] == [55, 55, 1, 5500]


def test_daily_collection_plan_rejects_invalid_estimates() -> None:
    with pytest.raises(ValueError):
        DailyCollectionPlan(
            ProviderCode.EASTMONEY,
            DailyCollectionMode.PAGED,
            total_symbols=0,
            group_size=100,
            estimated_seconds_per_request=0.5,
        )


def test_eastmoney_normalizes_a_paged_market_snapshot() -> None:
    class Client:
        async def request_json(self, request, *, deadline):
            del request, deadline
            return {
                "rc": 0,
                "data": {
                    "total": 1,
                    "diff": [
                        {
                            "f2": 10.5,
                            "f5": 12350,
                            "f6": 456700,
                            "f12": "600000",
                            "f15": 11,
                            "f16": 9,
                            "f17": 10,
                            "f18": 9.8,
                        }
                    ],
                },
            }

    result = asyncio.run(
        EastmoneyProvider(Client()).market_daily_bars(
            MarketDailyGroupRequest(date(2026, 7, 30), ("600000.SH",), 0),
            deadline(),
        )
    )
    assert result.items[0].close == Decimal("10.5")
    assert result.items[0].capability is ProviderCapability.DAILY_BAR_UNADJUSTED


def test_eastmoney_isolates_an_invalid_stock_inside_a_page() -> None:
    class Client:
        async def request_json(self, request, *, deadline):
            del request, deadline
            base = {"f5": 100, "f6": 1000, "f15": 11, "f16": 9, "f17": 10}
            return {
                "rc": 0,
                "data": {
                    "diff": [
                        {**base, "f2": 10.5, "f12": "600000"},
                        {**base, "f2": "-", "f12": "000001"},
                    ]
                },
            }

    result = asyncio.run(
        EastmoneyProvider(Client()).market_daily_bars(
            MarketDailyGroupRequest(
                date(2026, 7, 30), ("600000.SH", "000001.SZ"), 0
            ),
            deadline(),
        )
    )

    assert [item.symbol for item in result.items] == ["600000.SH"]
    assert [(item.symbol, item.code) for item in result.failures] == [
        ("000001.SZ", "PROVIDER_ITEM_SCHEMA_INVALID")
    ]


def test_sina_builds_daily_bar_only_from_same_day_quote() -> None:
    china = timezone(timedelta(hours=8))

    class Provider(SinaRealtimeProvider):
        async def realtime_quotes(self, symbols, deadline):
            del deadline
            return ProviderBatchResult(
                tuple(
                    RealtimeQuote(
                        symbol=symbol,
                        price=Decimal("10.5"),
                        open=Decimal("10"),
                        high=Decimal("11"),
                        low=Decimal("9"),
                        previous_close=Decimal("9.8"),
                        volume=12350,
                        amount=Decimal("456700"),
                        quote_time=datetime(2026, 7, 30, 15, tzinfo=china),
                        received_at=datetime.now(UTC),
                        source=ProviderCode.SINA,
                    )
                    for symbol in symbols
                )
            )

    result = asyncio.run(
        Provider(None).market_daily_bars(
            MarketDailyGroupRequest(date(2026, 7, 30), ("600000.SH",), 0),
            deadline(),
        )
    )
    assert [item.symbol for item in result.items] == ["600000.SH"]


def test_tushare_snapshot_filters_to_frozen_scope() -> None:
    async def token() -> str:
        return "test"

    def loader(**kwargs):
        assert kwargs["trade_date"] == "20260730"
        return pd.DataFrame(
            [
                {
                    "ts_code": "600000.SH",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 123.5,
                    "amount": 456.7,
                },
                {
                    "ts_code": "000001.SZ",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 123.5,
                    "amount": 456.7,
                },
            ]
        )

    result = asyncio.run(
        TushareProvider(token_resolver=token, sdk_loader=loader).market_daily_bars(
            MarketDailyGroupRequest(date(2026, 7, 30), ("600000.SH",), 0),
            deadline(),
        )
    )
    assert [item.symbol for item in result.items] == ["600000.SH"]


def test_baostock_single_mode_rejects_larger_group() -> None:
    with pytest.raises(ProviderHttpError, match="PROVIDER_GROUP_SIZE_INVALID"):
        asyncio.run(
            BaoStockProvider().market_daily_bars(
                MarketDailyGroupRequest(
                    date(2026, 7, 30), ("600000.SH", "000001.SZ"), 0
                ),
                deadline(),
            )
        )
