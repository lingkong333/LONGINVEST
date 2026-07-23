from datetime import UTC, datetime
from pathlib import Path

import pytest

from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.modules.providers.sina import SinaRealtimeProvider

FIXTURES = Path(__file__).parent / "fixtures" / "sina"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_sina_declares_realtime_and_security_master_endpoints() -> None:
    assert SinaRealtimeProvider.code is ProviderCode.SINA
    assert SinaRealtimeProvider.capabilities == frozenset(
        {
            ProviderCapability.SECURITY_MASTER,
            ProviderCapability.REALTIME_QUOTE_BATCH,
        }
    )
    assert SinaRealtimeProvider.REALTIME_URL.startswith("https://hq.sinajs.cn/")
    assert SinaRealtimeProvider.MASTER_PAGE_URL.startswith(
        "https://vip.stock.finance.sina.com.cn/"
    )


def test_sina_parses_security_master_page_for_three_markets() -> None:
    records = SinaRealtimeProvider.parse_security_master_page(
        """[
          {"symbol":"sh600000","code":"600000","name":"浦发银行"},
          {"symbol":"sz000001","code":"000001","name":"平安银行"},
          {"symbol":"bj920000","code":"920000","name":"安徽凤凰"}
        ]""",
        observed_at=datetime.now(UTC),
    )

    assert [record.symbol for record in records] == [
        "600000.SH",
        "000001.SZ",
        "920000.BJ",
    ]
    assert all(record.security_type == "A_SHARE" for record in records)
    assert all(record.source is ProviderCode.SINA for record in records)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '[{"symbol":"sh600000","code":"000001","name":"错误映射"}]',
        (
            '[{"symbol":"sh600000","code":"600000","name":"浦发银行"},'
            '{"symbol":"sh600000","code":"600000","name":"重复"}]'
        ),
    ],
)
def test_sina_rejects_empty_invalid_or_duplicate_security_master(payload: str) -> None:
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        SinaRealtimeProvider.parse_security_master_page(
            payload, observed_at=datetime.now(UTC)
        )


def test_sina_normalizes_quotes_and_multiple_markets() -> None:
    result = SinaRealtimeProvider(None).parse_quotes(
        load("multi_market.txt"),
        ("600000.SH", "000001.SZ", "430047.BJ"),
        received_at=datetime.now(UTC),
    )
    assert [item.symbol for item in result.items] == [
        "600000.SH",
        "000001.SZ",
        "430047.BJ",
    ]
    assert all(item.source is ProviderCode.SINA for item in result.items)


def test_sina_empty_and_partial_are_item_failures() -> None:
    provider = SinaRealtimeProvider(None)
    empty = provider.parse_quotes(
        load("empty.txt"), ("600000.SH",), received_at=datetime.now(UTC)
    )
    partial = provider.parse_quotes(
        load("partial.txt"),
        ("600000.SH", "000001.SZ"),
        received_at=datetime.now(UTC),
    )
    assert empty.failures[0].code == "PROVIDER_ITEM_MISSING"
    assert partial.failures[0].symbol == "000001.SZ"


@pytest.mark.parametrize(
    "fixture",
    [
        "error.txt",
        "html.txt",
        "captcha.txt",
    ],
)
def test_sina_schema_anomalies_have_stable_error(fixture: str) -> None:
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        SinaRealtimeProvider(None).parse_quotes(
            load(fixture), ("600000.SH",), received_at=datetime.now(UTC)
        )


def test_sina_isolates_identifiable_bad_row_in_mixed_batch() -> None:
    text = load("multi_market.txt").replace(
        'var hq_str_sz000001="深市,10,10,10,10,10,10,10,1,10',
        'var hq_str_sz000001="深市,10,10,-,10,10,10,10,1,10',
    )
    result = SinaRealtimeProvider(None).parse_quotes(
        text,
        ("600000.SH", "000001.SZ", "430047.BJ"),
        received_at=datetime.now(UTC),
    )
    assert [item.symbol for item in result.items] == ["600000.SH", "430047.BJ"]
    assert result.failures[0].symbol == "000001.SZ"
    assert result.failures[0].code == "PROVIDER_ITEM_INVALID"


@pytest.mark.parametrize(
    "fixture", ["missing_fields.txt", "bad_time.txt", "oversize.txt"]
)
def test_sina_identifiable_anomaly_is_an_item_failure(fixture: str) -> None:
    result = SinaRealtimeProvider(None).parse_quotes(
        load(fixture), ("600000.SH",), received_at=datetime.now(UTC)
    )
    assert result.items == ()
    assert result.failures[0].code == "PROVIDER_ITEM_INVALID"
