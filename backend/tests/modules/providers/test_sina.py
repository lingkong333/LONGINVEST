import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from long_invest.modules.providers.contracts import (
    DailyBarRequest,
    ProviderCapability,
    ProviderCode,
)
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.modules.providers.sina import SinaRealtimeProvider

FIXTURES = Path(__file__).parent / "fixtures" / "sina"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_sina_declares_realtime_master_and_history_capabilities() -> None:
    assert SinaRealtimeProvider.code is ProviderCode.SINA
    assert SinaRealtimeProvider.capabilities == frozenset(
        {
            ProviderCapability.SECURITY_MASTER,
            ProviderCapability.REALTIME_QUOTE_BATCH,
            ProviderCapability.DAILY_BAR_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_QFQ,
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
    assert all(record.listed is None for record in records)


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


def _history_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame.index = pd.to_datetime(frame.pop("date"))
    return frame


@pytest.mark.parametrize(
    ("capability", "adjust"),
    [
        (ProviderCapability.HISTORICAL_DAILY_UNADJUSTED, ""),
        (ProviderCapability.HISTORICAL_DAILY_QFQ, "qfq"),
    ],
)
def test_sina_loads_and_normalizes_historical_daily(capability, adjust) -> None:
    calls = []
    guard_calls = []

    class Guard:
        async def __aenter__(self):
            guard_calls.append("claim")

        async def __aexit__(self, *args):
            del args
            guard_calls.append("release")

    def loader(**kwargs):
        calls.append(kwargs)
        return _history_frame(
            [
                {
                    "date": "2026-07-24",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "volume": 100,
                    "amount": 1000,
                },
                {
                    "date": "2026-07-27",
                    "open": 10.5,
                    "high": 12,
                    "low": 10,
                    "close": 11,
                    "volume": 200,
                    "amount": 2200,
                },
            ]
        )

    request = DailyBarRequest(
        "600000.SH", date(2026, 7, 1), date(2026, 7, 27), capability
    )
    result = asyncio.run(
        SinaRealtimeProvider(
            None,
            history_loader=loader,
            request_guard=Guard,
        ).daily_bars(request, datetime.now(UTC) + timedelta(seconds=5))
    )

    assert calls == [
        {
            "symbol": "sh600000",
            "start_date": "20260701",
            "end_date": "20260727",
            "adjust": adjust,
        }
    ]
    assert guard_calls == ["claim", "release"]
    assert [item.trading_date for item in result.items] == [
        date(2026, 7, 24),
        date(2026, 7, 27),
    ]
    assert result.items[-1].close == Decimal("11")
    assert all(item.source is ProviderCode.SINA for item in result.items)
    assert all(item.capability is capability for item in result.items)


def test_sina_accepts_akshare_date_column_shape() -> None:
    request = DailyBarRequest(
        "000001.SZ",
        date(2026, 7, 24),
        date(2026, 7, 27),
        ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
    )
    frame = pd.DataFrame(
        [
            {
                "date": date(2026, 7, 24),
                "open": 11.09,
                "high": 11.18,
                "low": 11.09,
                "close": 11.10,
                "volume": 114093292.0,
                "amount": 1269361000.0,
            },
            {
                "date": date(2026, 7, 27),
                "open": 11.11,
                "high": 11.16,
                "low": 11.04,
                "close": 11.11,
                "volume": 95715556.0,
                "amount": 1062796000.0,
            },
        ]
    )

    result = SinaRealtimeProvider.parse_daily_bars(frame, request=request)

    assert [item.trading_date for item in result.items] == [
        date(2026, 7, 24),
        date(2026, 7, 27),
    ]
    assert result.items[-1].volume == 95715556


def test_sina_qfq_discards_only_the_nonpositive_prefix() -> None:
    request = DailyBarRequest(
        "000001.SZ",
        date(1991, 1, 1),
        date(1991, 1, 4),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    frame = _history_frame(
        [
            {
                "date": "1991-01-01",
                "open": -1,
                "high": 0,
                "low": -2,
                "close": -1,
                "volume": 1,
                "amount": 1,
            },
            {
                "date": "1991-01-02",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 1,
                "amount": 1,
            },
            {
                "date": "1991-01-03",
                "open": 2,
                "high": 3,
                "low": 2,
                "close": 3,
                "volume": 1,
                "amount": 1,
            },
        ]
    )

    result = SinaRealtimeProvider.parse_daily_bars(frame, request=request)

    assert [item.trading_date for item in result.items] == [
        date(1991, 1, 2),
        date(1991, 1, 3),
    ]


def test_sina_skips_nonpositive_qfq_inside_the_valid_suffix() -> None:
    request = DailyBarRequest(
        "000001.SZ",
        date(1991, 1, 1),
        date(1991, 1, 4),
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    frame = _history_frame(
        [
            {
                "date": "1991-01-01",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 1,
                "amount": 1,
            },
            {
                "date": "1991-01-02",
                "open": -1,
                "high": 0,
                "low": -2,
                "close": -1,
                "volume": 1,
                "amount": 1,
            },
        ]
    )

    result = SinaRealtimeProvider.parse_daily_bars(frame, request=request)

    assert [item.trading_date for item in result.items] == [date(1991, 1, 1)]
    assert len(result.anomalies) == 1
    assert result.anomalies[0].trading_date == date(1991, 1, 2)
    assert result.anomalies[0].code == "PROVIDER_DAILY_BAR_INVALID"
