import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from long_invest.modules.providers.contracts import (
    DailyBarRequest,
    ProviderCapability,
    ProviderCode,
)
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
    result = provider.parse_quotes(
        load("multi_market.json"),
        ("600000.SH", "000001.SZ", "430047.BJ"),
        received_at=datetime.now(UTC),
    )
    assert [item.symbol for item in result.items] == [
        "600000.SH",
        "000001.SZ",
        "430047.BJ",
    ]
    assert all(item.source is ProviderCode.EASTMONEY for item in result.items)


def test_eastmoney_empty_and_partial_are_item_failures() -> None:
    provider = EastmoneyProvider(None)
    empty = provider.parse_quotes(
        load("empty.json"), ("600000.SH",), received_at=datetime.now(UTC)
    )
    partial = provider.parse_quotes(
        load("partial.json"),
        ("600000.SH", "000001.SZ"),
        received_at=datetime.now(UTC),
    )
    assert empty.failures[0].code == "PROVIDER_ITEM_MISSING"
    assert [failure.symbol for failure in partial.failures] == ["000001.SZ"]


@pytest.mark.parametrize(
    "fixture",
    [
        "error_code.json",
        "html.json",
        "captcha.json",
        "oversize.json",
    ],
)
def test_eastmoney_schema_anomalies_have_stable_error(fixture: str) -> None:
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        EastmoneyProvider(None).parse_quotes(
            load(fixture), ("600000.SH",), received_at=datetime.now(UTC)
        )


def test_eastmoney_normalizes_unadjusted_and_qfq_bars() -> None:
    provider = EastmoneyProvider(None)
    for capability in (
        ProviderCapability.DAILY_BAR_UNADJUSTED,
        ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    ):
        request = DailyBarRequest(
            "600000.SH", date(2025, 7, 14), date(2025, 7, 15), capability
        )
        result = provider.parse_bars(load("bars.json"), request=request)
        assert len(result.items) == 2
        assert result.items[0].trading_date == date(2025, 7, 14)
        assert result.items[0].capability is capability


def test_eastmoney_isolates_identifiable_bad_quote_row() -> None:
    payload = load("multi_market.json")
    payload["data"]["diff"][1]["f2"] = "-"
    result = EastmoneyProvider(None).parse_quotes(
        payload,
        ("600000.SH", "000001.SZ", "430047.BJ"),
        received_at=datetime.now(UTC),
    )
    assert [item.symbol for item in result.items] == ["600000.SH", "430047.BJ"]
    assert [(item.symbol, item.code) for item in result.failures] == [
        ("000001.SZ", "PROVIDER_ITEM_INVALID")
    ]


def test_eastmoney_keeps_nineteen_good_items_when_one_of_twenty_is_bad() -> None:
    template = load("normal.json")["data"]["diff"][0]
    rows = []
    symbols = []
    for index in range(20):
        code = f"6000{index:02d}"
        symbols.append(f"{code}.SH")
        rows.append({**template, "f12": code})
    rows[9]["f2"] = "-"
    result = EastmoneyProvider(None).parse_quotes(
        {"rc": 0, "data": {"diff": rows}},
        tuple(symbols),
        received_at=datetime.now(UTC),
    )
    assert len(result.items) == 19
    assert result.failures[0].symbol == "600009.SH"


@pytest.mark.parametrize("fixture", ["missing_field.json", "bad_time.json"])
def test_eastmoney_identifiable_anomaly_is_an_item_failure(fixture: str) -> None:
    result = EastmoneyProvider(None).parse_quotes(
        load(fixture), ("600000.SH",), received_at=datetime.now(UTC)
    )
    assert result.items == ()
    assert result.failures[0].code == "PROVIDER_ITEM_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        {"rc": 0, "data": {"code": "000001", "klines": ["2025-07-14,9,10,10,9,1,1"]}},
        {"rc": 0, "data": {"code": "600000", "klines": ["2025-07-13,9,10,10,9,1,1"]}},
        {
            "rc": 0,
            "data": {
                "code": "600000",
                "klines": ["2025-07-14,9,10,10,9,1,1", "2025-07-14,9,10,10,9,1,1"],
            },
        },
    ],
)
def test_eastmoney_rejects_bar_code_range_or_duplicate_date(payload) -> None:
    request = DailyBarRequest(
        "600000.SH",
        date(2025, 7, 14),
        date(2025, 7, 15),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        EastmoneyProvider(None).parse_bars(payload, request=request)


def test_eastmoney_rejects_descending_bars() -> None:
    request = DailyBarRequest(
        "600000.SH",
        date(2025, 7, 14),
        date(2025, 7, 15),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    payload = {
        "rc": 0,
        "data": {
            "code": "600000",
            "klines": [
                "2025-07-15,9,10,10,9,1,1",
                "2025-07-14,9,10,10,9,1,1",
            ],
        },
    }
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        EastmoneyProvider(None).parse_bars(payload, request=request)


def test_eastmoney_marks_empty_or_missing_weekdays_for_suspension_check() -> None:
    request = DailyBarRequest(
        "600000.SH",
        date(2025, 7, 14),
        date(2025, 7, 15),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    result = EastmoneyProvider(None).parse_bars(
        {"rc": 0, "data": {"code": "600000", "klines": []}},
        request=request,
    )
    assert result.items == ()
    assert result.failures[0].code == "PROVIDER_TRADING_DATES_MISSING"


def test_security_master_preserves_unknown_status_instead_of_fabricating_normal() -> (
    None
):
    payload = {
        "rc": 0,
        "data": {
            "diff": [
                {"f12": "600000", "f14": "浦发银行", "f26": "19991110", "f2": "-"},
                {"f12": "000001", "f14": "平安银行", "f26": "-", "f2": "-"},
            ]
        },
    }
    records = EastmoneyProvider(None).parse_security_master(
        payload, observed_at=datetime(2025, 7, 15, tzinfo=UTC)
    )
    assert records[0].listed is True
    assert records[0].suspended is True
    assert records[1].listed is None
    assert records[1].suspended is None


def test_security_master_recognizes_star_st_and_explicit_delisting_date() -> None:
    payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "600000",
                    "f14": "*ST示例",
                    "f26": "19991110",
                    "f80": "20250714",
                    "f2": "-",
                }
            ]
        },
    }
    record = EastmoneyProvider(None).parse_security_master(
        payload, observed_at=datetime(2025, 7, 15, tzinfo=UTC)
    )[0]
    assert record.is_st is True
    assert record.delisted_on == date(2025, 7, 14)
    assert record.listed is False
    assert record.suspended is None
