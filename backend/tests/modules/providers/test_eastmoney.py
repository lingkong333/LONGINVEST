import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.eastmoney import EastmoneyProvider
from long_invest.modules.providers.retry import ProviderHttpError

FIXTURES = Path(__file__).parent / "fixtures" / "eastmoney"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_eastmoney_declares_supported_capabilities_and_real_hosts() -> None:
    assert EastmoneyProvider.code is ProviderCode.EASTMONEY
    assert EastmoneyProvider.REALTIME_URL.startswith("https://push2.eastmoney.com/")
    assert ProviderCapability.HISTORICAL_DAILY_QFQ in EastmoneyProvider.capabilities
    assert ProviderCapability.SECURITY_MASTER in EastmoneyProvider.capabilities


def test_eastmoney_normalizes_quotes_and_multiple_markets() -> None:
    provider = EastmoneyProvider(None)
    result = provider.parse_quotes(load("multi_market.json"), ("600000.SH", "000001.SZ", "430047.BJ"), received_at=datetime.now(timezone.utc))
    assert [item.symbol for item in result.items] == ["600000.SH", "000001.SZ", "430047.BJ"]
    assert all(item.source is ProviderCode.EASTMONEY for item in result.items)


def test_eastmoney_empty_and_partial_are_item_failures() -> None:
    provider = EastmoneyProvider(None)
    empty = provider.parse_quotes(load("empty.json"), ("600000.SH",), received_at=datetime.now(timezone.utc))
    partial = provider.parse_quotes(load("partial.json"), ("600000.SH", "000001.SZ"), received_at=datetime.now(timezone.utc))
    assert empty.failures[0].code == "PROVIDER_ITEM_MISSING"
    assert [failure.symbol for failure in partial.failures] == ["000001.SZ"]


@pytest.mark.parametrize("fixture", ["missing_field.json", "error_code.json", "bad_time.json", "html.json", "captcha.json", "oversize.json"])
def test_eastmoney_schema_anomalies_have_stable_error(fixture: str) -> None:
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        EastmoneyProvider(None).parse_quotes(load(fixture), ("600000.SH",), received_at=datetime.now(timezone.utc))


def test_eastmoney_normalizes_unadjusted_and_qfq_bars() -> None:
    provider = EastmoneyProvider(None)
    for capability in (
        ProviderCapability.DAILY_BAR_UNADJUSTED,
        ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    ):
        result = provider.parse_bars(load("bars.json"), symbol="600000.SH", capability=capability)
        assert len(result.items) == 2
        assert result.items[0].trading_date == date(2025, 7, 14)
        assert result.items[0].capability is capability
