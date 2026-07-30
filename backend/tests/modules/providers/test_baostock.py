import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from long_invest.modules.providers.baostock import BaoStockProvider
from long_invest.modules.providers.contracts import (
    DailyBarRequest,
    ProviderAdapterCode,
    ProviderCapability,
    ProviderCode,
)
from long_invest.modules.providers.retry import ProviderHttpError


def test_baostock_declares_real_sdk_capabilities_and_identity() -> None:
    provider = BaoStockProvider(sdk_loader=lambda **_: [])
    assert provider.code is ProviderCode.BAOSTOCK
    assert ProviderCapability.DAILY_BAR_UNADJUSTED in provider.capabilities
    identity = provider.source_identity(ProviderCapability.HISTORICAL_DAILY_QFQ)
    assert identity.adapter is ProviderAdapterCode.BAOSTOCK_SDK
    assert identity.interface == "baostock.query_history_k_data_plus"


def test_baostock_normalizes_master_and_skips_non_a_share_markets() -> None:
    records = BaoStockProvider.parse_security_master(
        [
            {"code": "sh.600000", "code_name": "浦发银行", "tradeStatus": "1"},
            {"code": "sz.000001", "code_name": "平安银行", "tradeStatus": "0"},
            {"code": "sh.000001", "code_name": "上证指数", "tradeStatus": "1"},
            {"code": "hk.000001", "code_name": "港股", "tradeStatus": "1"},
        ],
        observed_at=datetime.now(UTC),
    )
    assert [item.symbol for item in records] == ["000001.SZ", "600000.SH"]
    assert records[0].suspended


def test_baostock_normalizes_daily_and_ignores_suspension_rows() -> None:
    request = DailyBarRequest(
        "600000.SH",
        date(2026, 7, 1),
        date(2026, 7, 30),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    result = BaoStockProvider.parse_daily_bars(
        [
            {
                "date": "2026-07-29",
                "open": "",
                "high": "",
                "low": "",
                "close": "",
                "volume": "0",
                "amount": "0",
                "tradestatus": "0",
            },
            {
                "date": "2026-07-30",
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10.5",
                "volume": "12350",
                "amount": "456700",
                "tradestatus": "1",
            },
        ],
        request=request,
    )
    assert len(result.items) == 1
    assert result.items[0].volume == 12_350
    assert result.items[0].amount == Decimal("456700")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"code": "sh.600000", "code_name": "", "tradeStatus": "1"}],
        [
            {"code": "sh.600000", "code_name": "浦发银行", "tradeStatus": "1"},
            {"code": "sh.600000", "code_name": "重复", "tradeStatus": "1"},
        ],
    ],
)
def test_baostock_rejects_empty_changed_or_duplicate_master_schema(rows) -> None:
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        BaoStockProvider.parse_security_master(rows, observed_at=datetime.now(UTC))


def test_baostock_passes_adjustment_mode_and_safe_failure() -> None:
    calls = []

    def loader(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("upstream internal detail")

    provider = BaoStockProvider(sdk_loader=loader)
    request = DailyBarRequest(
        "600000.SH",
        date(2026, 7, 1),
        date(2026, 7, 30),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    with pytest.raises(ProviderHttpError, match="PROVIDER_UPSTREAM_FAILED"):
        asyncio.run(
            provider.daily_bars(
                request, datetime.now(UTC) + timedelta(seconds=1)
            )
        )
    assert calls[0]["adjustflag"] == "2"
