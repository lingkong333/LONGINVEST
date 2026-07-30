import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from long_invest.modules.providers.contracts import (
    DailyBarRequest,
    ProviderAdapterCode,
    ProviderCapability,
    ProviderCode,
)
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.modules.providers.tushare import TushareProvider


async def token() -> str:
    return "test-token"


def test_tushare_declares_real_sdk_capabilities_and_identity() -> None:
    provider = TushareProvider(token_resolver=token, sdk_loader=lambda **_: None)
    assert provider.code is ProviderCode.TUSHARE
    assert ProviderCapability.DAILY_BAR_UNADJUSTED in provider.capabilities
    identity = provider.source_identity(ProviderCapability.HISTORICAL_DAILY_QFQ)
    assert identity.adapter is ProviderAdapterCode.TUSHARE_SDK
    assert identity.interface == "tushare.pro_bar"


def test_tushare_normalizes_master_and_daily_units() -> None:
    master = TushareProvider.parse_security_master(
        [
            pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "name": "浦发银行",
                        "list_date": "19991110",
                        "delist_date": None,
                    },
                    {
                        "ts_code": "920000.BJ",
                        "name": "北交示例",
                        "list_date": "20200101",
                        "delist_date": "",
                    },
                ]
            )
        ],
        observed_at=datetime.now(UTC),
    )
    assert [item.symbol for item in master] == ["600000.SH", "920000.BJ"]

    request = DailyBarRequest(
        "600000.SH",
        date(2026, 7, 1),
        date(2026, 7, 30),
        ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
    )
    result = TushareProvider.parse_daily_bars(
        pd.DataFrame(
            [
                {
                    "trade_date": "20260730",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 123.5,
                    "amount": 456.7,
                }
            ]
        ),
        request=request,
    )
    assert result.items[0].volume == 12_350
    assert result.items[0].amount == Decimal("456700.0")


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame(),
        pd.DataFrame([{"ts_code": "600000.SH", "name": ""}]),
        pd.DataFrame([{"ts_code": "INVALID", "name": "坏数据"}]),
    ],
)
def test_tushare_rejects_empty_or_changed_master_schema(frame) -> None:
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        TushareProvider.parse_security_master(
            [frame], observed_at=datetime.now(UTC)
        )


def test_tushare_requires_secret_without_leaking_it() -> None:
    async def missing() -> None:
        return None

    provider = TushareProvider(token_resolver=missing)
    result = asyncio.run(
        provider.probe(
            ProviderCapability.SECURITY_MASTER,
            datetime.now(UTC) + timedelta(seconds=1),
        )
    )
    assert not result.healthy
    assert result.error_code == "PROVIDER_CREDENTIAL_UNAVAILABLE"
    assert "token" not in repr(result)


def test_tushare_converts_sdk_failure_to_safe_error() -> None:
    def failed(**kwargs):
        del kwargs
        raise RuntimeError("secret upstream detail")

    provider = TushareProvider(token_resolver=token, sdk_loader=failed)
    with pytest.raises(ProviderHttpError, match="PROVIDER_UPSTREAM_FAILED"):
        asyncio.run(
            provider.security_master(datetime.now(UTC) + timedelta(seconds=1))
        )


def test_tushare_enforces_total_deadline() -> None:
    def slow(**kwargs):
        del kwargs
        time.sleep(0.05)
        return pd.DataFrame()

    provider = TushareProvider(token_resolver=token, sdk_loader=slow)
    with pytest.raises(TimeoutError):
        asyncio.run(
            provider.security_master(
                datetime.now(UTC) + timedelta(milliseconds=5)
            )
        )
