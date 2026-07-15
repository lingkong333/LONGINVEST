from datetime import datetime, timezone
from pathlib import Path

import pytest

from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.modules.providers.sina import SinaRealtimeProvider

FIXTURES = Path(__file__).parent / "fixtures" / "sina"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_sina_declares_only_realtime_and_real_endpoint() -> None:
    assert SinaRealtimeProvider.code is ProviderCode.SINA
    assert SinaRealtimeProvider.capabilities == frozenset({ProviderCapability.REALTIME_QUOTE_BATCH})
    assert SinaRealtimeProvider.REALTIME_URL.startswith("https://hq.sinajs.cn/")


def test_sina_normalizes_quotes_and_multiple_markets() -> None:
    result = SinaRealtimeProvider(None).parse_quotes(
        load("multi_market.txt"), ("600000.SH", "000001.SZ", "430047.BJ"), received_at=datetime.now(timezone.utc)
    )
    assert [item.symbol for item in result.items] == ["600000.SH", "000001.SZ", "430047.BJ"]
    assert all(item.source is ProviderCode.SINA for item in result.items)


def test_sina_empty_and_partial_are_item_failures() -> None:
    provider = SinaRealtimeProvider(None)
    empty = provider.parse_quotes(load("empty.txt"), ("600000.SH",), received_at=datetime.now(timezone.utc))
    partial = provider.parse_quotes(load("partial.txt"), ("600000.SH", "000001.SZ"), received_at=datetime.now(timezone.utc))
    assert empty.failures[0].code == "PROVIDER_ITEM_MISSING"
    assert partial.failures[0].symbol == "000001.SZ"


@pytest.mark.parametrize("fixture", ["missing_fields.txt", "error.txt", "html.txt", "captcha.txt", "bad_time.txt", "oversize.txt"])
def test_sina_schema_anomalies_have_stable_error(fixture: str) -> None:
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        SinaRealtimeProvider(None).parse_quotes(load(fixture), ("600000.SH",), received_at=datetime.now(timezone.utc))
