import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from long_invest.modules.providers.contracts import (
    CorporateActionRequest,
    CorporateActionType,
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
    assert ProviderCapability.CORPORATE_ACTIONS in EastmoneyProvider.capabilities


class CorporateActionClient:
    def __init__(self, *, content_matches: bool = True) -> None:
        self.content_matches = content_matches
        self.requests = []

    async def request_json(self, request, *, deadline):
        del deadline
        self.requests.append(request)
        if request.url == EastmoneyProvider.CORPORATE_ACTION_URL:
            if request.params["reportName"] == "RPT_IPO_ALLOTMENT":
                return {
                    "version": None,
                    "result": None,
                    "success": False,
                    "message": "empty",
                    "code": 9201,
                }
            return {
                "version": "v1",
                "result": {
                    "pages": 1,
                    "count": 1,
                    "data": [
                        {
                            "SECUCODE": "300033.SZ",
                            "SECURITY_CODE": "300033",
                            "BONUS_RATIO": None,
                            "IT_RATIO": 4,
                            "PRETAX_BONUS_RMB": 51,
                            "NOTICE_DATE": "2025-04-02 00:00:00",
                            "EQUITY_RECORD_DATE": "2025-04-09 00:00:00",
                            "EX_DIVIDEND_DATE": "2025-04-10 00:00:00",
                            "REPORT_DATE": "2024-12-31 00:00:00",
                            "ASSIGN_PROGRESS": "实施分配",
                        }
                    ],
                },
                "success": True,
                "code": 0,
            }
        if request.url == EastmoneyProvider.ANNOUNCEMENT_URL:
            return {
                "data": {
                    "list": [
                        {
                            "art_code": "AN1",
                            "columns": [
                                {"column_code": "001002002001005"}
                            ],
                            "display_time": "2025-04-02 18:19:23:211",
                            "eiTime": "2025-04-02 18:20:42:000",
                            "title": (
                                "示例:2024年度利润分配及资本公积金"
                                "转增股本实施公告"
                            ),
                        }
                    ],
                    "total_hits": 1,
                },
                "success": 1,
            }
        if request.url == EastmoneyProvider.ANNOUNCEMENT_CONTENT_URL:
            terms = (
                "每10股派51.000000元，同时每10股转增4.000000股。"
                if self.content_matches
                else "每10股派50元，同时每10股转增4股。"
            )
            return {
                "data": {
                    "art_code": "AN1",
                    "notice_content": (
                        f"{terms}股权登记日为2025年4月9日；"
                        "除权除息日为2025年4月10日。"
                    ),
                },
                "success": 1,
            }
        if request.url == EastmoneyProvider.HISTORY_URL:
            return {
                "rc": 0,
                "data": {
                    "code": "300033",
                    "klines": ["2025-04-09,99,100,101,98,1,100"],
                },
            }
        raise AssertionError(request.url)


def test_corporate_actions_builds_verified_dividend_factor() -> None:
    client = CorporateActionClient()
    request = CorporateActionRequest(
        "300033.SZ", date(2025, 4, 10), date(2025, 4, 10)
    )
    result = asyncio.run(
        EastmoneyProvider(client).corporate_actions(
            request, datetime.now(UTC) + timedelta(seconds=5)
        )
    )
    assert len(result.items) == 1
    item = result.items[0]
    assert item.event_type is CorporateActionType.COMPOSITE
    assert item.source_event_id == "AN1"
    assert item.adjustment_factor == (
        Decimal("100") - Decimal("5.1")
    ) / Decimal("140")
    assert item.published_at == datetime(2025, 4, 2, 10, 20, 42, tzinfo=UTC)
    assert len(item.raw_payload_hash) == 64


def test_corporate_actions_rejects_announcement_terms_that_do_not_match() -> None:
    request = CorporateActionRequest(
        "300033.SZ", date(2025, 4, 10), date(2025, 4, 10)
    )
    with pytest.raises(ProviderHttpError, match="ADJUSTMENT_DATA_UNAVAILABLE"):
        asyncio.run(
            EastmoneyProvider(
                CorporateActionClient(content_matches=False)
            ).corporate_actions(request, datetime.now(UTC) + timedelta(seconds=5))
        )


class IncompletePaginationClient:
    async def request_json(self, request, *, deadline):
        del deadline
        return {
            "result": {
                "pages": 2,
                "count": 2,
                "data": [{}] if request.params["pageNumber"] == "1" else [],
            },
            "success": True,
            "code": 0,
        }


def test_corporate_action_report_rejects_missing_page_rows() -> None:
    provider = EastmoneyProvider(IncompletePaginationClient())
    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        asyncio.run(
            provider._report_rows(
                "RPT_SHAREBONUS_DET",
                "PLAN_NOTICE_DATE",
                "300033.SZ",
                datetime.now(UTC) + timedelta(seconds=5),
            )
        )


def test_rights_evidence_requires_issue_terms_and_actual_result_quantity() -> None:
    EastmoneyProvider._validate_rights_content(
        (
            "股权登记日2022年1月18日，按每10股配售1.5股，"
            "配股价格为14.43元/股。"
        ),
        "实际发行1,552,021,645股。",
        event_date=date(2022, 1, 18),
        ratio_per_ten=Decimal("1.5"),
        issue_price=Decimal("14.43"),
        issue_num=Decimal("1552021645"),
    )


def test_rights_action_uses_issue_and_result_evidence_for_factor() -> None:
    class RightsProvider(EastmoneyProvider):
        async def _matching_rights_announcements(
            self, symbol, target_date, column, title_suffix, deadline
        ):
            del symbol, target_date, title_suffix, deadline
            return [
                {
                    "art_code": "ISSUE" if column.endswith("004") else "RESULT",
                    "display_time": "",
                    "eiTime": (
                        "2022-01-13 19:09:43:000"
                        if column.endswith("004")
                        else "2022-01-26 16:07:13:000"
                    ),
                }
            ]

        async def _announcement_content(self, announcement, deadline):
            del deadline
            if announcement["art_code"] == "ISSUE":
                return (
                    "股权登记日2022年1月18日，按每10股配售1.5股，"
                    "配股价格为14.43元/股。"
                )
            return "实际发行1,552,021,645股。"

        async def _previous_close(self, symbol, effective_date, deadline):
            del symbol, effective_date, deadline
            return Decimal("20")

    row = {
        "SECUCODE": "600030.SH",
        "SECURITY_CODE": "600030",
        "PLACING_RATIO": 1.5,
        "ISSUE_PRICE": 14.43,
        "ISSUE_NUM": 1552021645,
        "EQUITY_RECORD_DATE": "2022-01-18 00:00:00",
        "EX_DIVIDEND_DATE": "2022-01-27 00:00:00",
        "FIRST_NOTICE_DATE": "2022-01-14 00:00:00",
    }
    item = asyncio.run(
        RightsProvider(None)._rights_action(
            row,
            CorporateActionRequest(
                "600030.SH", date(2022, 1, 27), date(2022, 1, 27)
            ),
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime.now(UTC) + timedelta(seconds=5),
        )
    )
    assert item.event_type is CorporateActionType.RIGHTS_ISSUE
    assert item.source_event_id == "ISSUE+RESULT"
    assert item.adjustment_factor == Decimal("22.1645") / Decimal("23")


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
