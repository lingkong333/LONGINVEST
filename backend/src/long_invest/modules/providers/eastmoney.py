from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from long_invest.modules.providers.contracts import (
    CorporateActionRecord,
    CorporateActionRequest,
    CorporateActionType,
    DailyBar,
    DailyBarRequest,
    ProbeResult,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
    RealtimeQuote,
    SecurityMasterRecord,
)
from long_invest.modules.providers.http_client import (
    ProviderHttpClient,
    ProviderHttpRequest,
)
from long_invest.modules.providers.retry import ProviderHttpError


def _symbol(code: str) -> str:
    if len(code) != 6 or not code.isdigit():
        raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value in (None, "", "-"):
        raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error


class EastmoneyProvider:
    code = ProviderCode.EASTMONEY
    capabilities = frozenset(
        {
            ProviderCapability.CORPORATE_ACTIONS,
            ProviderCapability.SECURITY_MASTER,
            ProviderCapability.REALTIME_QUOTE_BATCH,
            ProviderCapability.DAILY_BAR_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_QFQ,
        }
    )
    REALTIME_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    MASTER_URL = "https://push2.eastmoney.com/api/qt/clist/get"
    MASTER_PROBE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
    HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    CORPORATE_ACTION_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    ANNOUNCEMENT_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    ANNOUNCEMENT_CONTENT_URL = (
        "https://np-cnotice-stock.eastmoney.com/api/content/ann"
    )
    _CHINA = ZoneInfo("Asia/Shanghai")

    def __init__(self, client: ProviderHttpClient | None) -> None:
        self._client = client

    async def security_master(
        self, deadline: datetime
    ) -> tuple[SecurityMasterRecord, ...]:
        payload = await self._json(
            self.MASTER_URL,
            {
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f12,f14,f26,f80",
                "pz": "10000",
            },
            deadline,
        )
        try:
            return self.parse_security_master(payload, observed_at=datetime.now(UTC))
        except ProviderHttpError as error:
            self._attach_schema_sample(error, payload)
            raise

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        secids = ",".join(
            ("1." if symbol.endswith(".SH") else "0.") + symbol[:6]
            for symbol in symbols
        )
        payload = await self._json(
            self.REALTIME_URL,
            {"secids": secids, "fields": "f2,f5,f6,f12,f14,f15,f16,f17,f18,f124"},
            deadline,
        )
        try:
            return self.parse_quotes(payload, symbols, received_at=datetime.now(UTC))
        except ProviderHttpError as error:
            self._attach_schema_sample(error, payload)
            raise

    async def daily_bars(
        self, request: DailyBarRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]:
        secid = ("1." if request.symbol.endswith(".SH") else "0.") + request.symbol[:6]
        fqt = (
            "1"
            if request.capability is ProviderCapability.HISTORICAL_DAILY_QFQ
            else "0"
        )
        payload = await self._json(
            self.HISTORY_URL,
            {
                "secid": secid,
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "klt": "101",
                "fqt": fqt,
                "beg": request.start.strftime("%Y%m%d"),
                "end": request.end.strftime("%Y%m%d"),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            },
            deadline,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Connection": "close",
                "Referer": "https://quote.eastmoney.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
                ),
            },
        )
        try:
            return self.parse_bars(payload, request=request)
        except ProviderHttpError as error:
            self._attach_schema_sample(error, payload)
            raise

    async def corporate_actions(
        self, request: CorporateActionRequest, deadline: datetime
    ) -> ProviderBatchResult[CorporateActionRecord]:
        observed_at = datetime.now(UTC)
        try:
            dividend_rows = await self._report_rows(
                "RPT_SHAREBONUS_DET",
                "PLAN_NOTICE_DATE",
                request.symbol,
                deadline,
            )
            rights_rows = await self._report_rows(
                "RPT_IPO_ALLOTMENT",
                "EQUITY_RECORD_DATE",
                request.symbol,
                deadline,
            )
            candidates: list[tuple[str, dict[str, Any]]] = []
            for kind, rows in (("dividend", dividend_rows), ("rights", rights_rows)):
                for row in rows:
                    self._validate_action_symbol(row, request.symbol)
                    if kind == "dividend" and row.get("ASSIGN_PROGRESS") != "实施分配":
                        continue
                    effective_date = self._row_date(row, "EX_DIVIDEND_DATE")
                    if request.start <= effective_date <= request.end:
                        candidates.append((kind, row))
            effective_dates = [
                self._row_date(row, "EX_DIVIDEND_DATE") for _, row in candidates
            ]
            if len(effective_dates) != len(set(effective_dates)):
                raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")

            items = []
            for kind, row in candidates:
                if kind == "dividend":
                    item = await self._dividend_action(
                        row, request, observed_at, deadline
                    )
                else:
                    item = await self._rights_action(
                        row, request, observed_at, deadline
                    )
                items.append(item)
            return ProviderBatchResult(
                tuple(sorted(items, key=lambda item: item.effective_date))
            )
        except ProviderHttpError:
            raise
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error

    async def _report_rows(
        self,
        report_name: str,
        sort_column: str,
        symbol: str,
        deadline: datetime,
    ) -> list[dict[str, Any]]:
        page = 1
        expected_pages: int | None = None
        expected_count: int | None = None
        rows: list[dict[str, Any]] = []
        while expected_pages is None or page <= expected_pages:
            payload = await self._json(
                self.CORPORATE_ACTION_URL,
                {
                    "reportName": report_name,
                    "columns": "ALL",
                    "quoteColumns": "",
                    "pageNumber": str(page),
                    "pageSize": "50",
                    "sortColumns": sort_column,
                    "sortTypes": "-1",
                    "source": "WEB",
                    "client": "WEB",
                    "filter": f'(SECURITY_CODE="{symbol[:6]}")',
                },
                deadline,
            )
            if payload.get("code") == 9201 and payload.get("result") is None:
                if page != 1:
                    raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
                return []
            result = payload.get("result")
            if (
                payload.get("success") is not True
                or payload.get("code") != 0
                or not isinstance(result, dict)
                or not isinstance(result.get("data"), list)
                or not isinstance(result.get("pages"), int)
                or not isinstance(result.get("count"), int)
                or result["pages"] < 1
                or result["count"] < 0
            ):
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            if expected_pages is None:
                expected_pages = result["pages"]
                expected_count = result["count"]
            elif (expected_pages, expected_count) != (
                result["pages"],
                result["count"],
            ):
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            if not all(isinstance(row, dict) for row in result["data"]):
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            rows.extend(result["data"])
            page += 1
        if expected_count != len(rows):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return rows

    async def _dividend_action(
        self,
        row: dict[str, Any],
        request: CorporateActionRequest,
        observed_at: datetime,
        deadline: datetime,
    ) -> CorporateActionRecord:
        self._validate_action_symbol(row, request.symbol)
        if row.get("ASSIGN_PROGRESS") != "实施分配":
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")
        effective_date = self._row_date(row, "EX_DIVIDEND_DATE")
        event_date = self._row_date(row, "EQUITY_RECORD_DATE")
        notice_date = self._row_date(row, "NOTICE_DATE")
        report_year = self._row_date(row, "REPORT_DATE").year
        cash_per_ten = self._optional_decimal(row, "PRETAX_BONUS_RMB")
        bonus_per_ten = self._optional_decimal(row, "BONUS_RATIO")
        transfer_per_ten = self._optional_decimal(row, "IT_RATIO")
        if cash_per_ten == bonus_per_ten == transfer_per_ten == 0:
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")

        announcements = await self._announcements(
            request.symbol,
            notice_date - timedelta(days=1),
            notice_date + timedelta(days=1),
            deadline,
        )
        matches = [
            item
            for item in announcements
            if self._announcement_has_column(item, "001002002001005")
            and str(report_year) in str(item.get("title", ""))
            and "实施公告" in str(item.get("title", ""))
            and any(
                phrase in str(item.get("title", ""))
                for phrase in ("权益分派", "利润分配", "分红派息")
            )
        ]
        if len(matches) != 1:
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")
        announcement = matches[0]
        content = await self._announcement_content(announcement, deadline)
        self._validate_dividend_content(
            content,
            event_date=event_date,
            effective_date=effective_date,
            cash_per_ten=cash_per_ten,
            bonus_per_ten=bonus_per_ten,
            transfer_per_ten=transfer_per_ten,
        )
        published_at = self._publication_time(announcement)
        self._validate_publication(published_at, effective_date, observed_at)
        previous_close = await self._previous_close(
            request.symbol, effective_date, deadline
        )
        factor = (
            previous_close - cash_per_ten / Decimal(10)
        ) / (
            previous_close
            * (
                Decimal(1)
                + bonus_per_ten / Decimal(10)
                + transfer_per_ten / Decimal(10)
            )
        )
        event_type = self._dividend_event_type(
            cash_per_ten, bonus_per_ten, transfer_per_ten
        )
        return self._action_record(
            request.symbol,
            announcement,
            event_type,
            event_date,
            effective_date,
            published_at,
            observed_at,
            factor,
            row,
            content,
            previous_close,
        )

    async def _rights_action(
        self,
        row: dict[str, Any],
        request: CorporateActionRequest,
        observed_at: datetime,
        deadline: datetime,
    ) -> CorporateActionRecord:
        self._validate_action_symbol(row, request.symbol)
        effective_date = self._row_date(row, "EX_DIVIDEND_DATE")
        event_date = self._row_date(row, "EQUITY_RECORD_DATE")
        first_notice_date = self._row_date(row, "FIRST_NOTICE_DATE")
        ratio_per_ten = self._required_positive_decimal(row, "PLACING_RATIO")
        issue_price = self._required_positive_decimal(row, "ISSUE_PRICE")
        issue_num = self._required_positive_decimal(row, "ISSUE_NUM")

        issue_matches = await self._matching_rights_announcements(
            request.symbol,
            first_notice_date,
            "001002001003004",
            "发行公告",
            deadline,
        )
        result_matches = await self._matching_rights_announcements(
            request.symbol,
            effective_date,
            "001002001003006",
            "发行结果公告",
            deadline,
        )
        if len(issue_matches) != 1 or len(result_matches) != 1:
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")
        evidence = issue_matches[0], result_matches[0]
        contents = [
            await self._announcement_content(item, deadline) for item in evidence
        ]
        self._validate_rights_content(
            contents[0],
            contents[1],
            event_date=event_date,
            ratio_per_ten=ratio_per_ten,
            issue_price=issue_price,
            issue_num=issue_num,
        )
        published_at = max(self._publication_time(item) for item in evidence)
        self._validate_publication(published_at, effective_date, observed_at)
        previous_close = await self._previous_close(
            request.symbol, effective_date, deadline
        )
        ratio_per_share = ratio_per_ten / Decimal(10)
        factor = (
            previous_close + ratio_per_share * issue_price
        ) / (previous_close * (Decimal(1) + ratio_per_share))
        combined_announcement = {
            "art_code": "+".join(str(item["art_code"]) for item in evidence),
            "evidence": evidence,
        }
        return self._action_record(
            request.symbol,
            combined_announcement,
            CorporateActionType.RIGHTS_ISSUE,
            event_date,
            effective_date,
            published_at,
            observed_at,
            factor,
            row,
            "\n".join(contents),
            previous_close,
        )

    async def _matching_rights_announcements(
        self,
        symbol: str,
        target_date: date,
        column: str,
        title_suffix: str,
        deadline: datetime,
    ) -> list[dict[str, Any]]:
        announcements = await self._announcements(
            symbol,
            target_date - timedelta(days=1),
            target_date + timedelta(days=1),
            deadline,
        )
        return [
            item
            for item in announcements
            if self._announcement_has_column(item, column)
            and "配股" in str(item.get("title", ""))
            and title_suffix in str(item.get("title", ""))
        ]

    async def _announcements(
        self, symbol: str, start: date, end: date, deadline: datetime
    ) -> list[dict[str, Any]]:
        page = 1
        rows: list[dict[str, Any]] = []
        total_hits: int | None = None
        while total_hits is None or len(rows) < total_hits:
            payload = await self._json(
                self.ANNOUNCEMENT_URL,
                {
                    "ann_type": "A",
                    "client_source": "web",
                    "stock_list": symbol[:6],
                    "page_index": str(page),
                    "page_size": "100",
                    "begin_time": start.isoformat(),
                    "end_time": end.isoformat(),
                },
                deadline,
            )
            data = payload.get("data")
            if (
                payload.get("success") != 1
                or not isinstance(data, dict)
                or not isinstance(data.get("list"), list)
                or not isinstance(data.get("total_hits"), int)
                or data["total_hits"] < 0
            ):
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            if total_hits is None:
                total_hits = data["total_hits"]
            elif total_hits != data["total_hits"]:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            page_rows = data["list"]
            if not all(isinstance(item, dict) for item in page_rows):
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            rows.extend(page_rows)
            if not page_rows and len(rows) < total_hits:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            page += 1
        if len(rows) != total_hits:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return rows

    async def _announcement_content(
        self, announcement: dict[str, Any], deadline: datetime
    ) -> str:
        art_code = announcement.get("art_code")
        if not isinstance(art_code, str) or not art_code:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        payload = await self._json(
            self.ANNOUNCEMENT_CONTENT_URL,
            {
                "art_code": art_code,
                "client_source": "web",
                "page_index": "1",
            },
            deadline,
        )
        data = payload.get("data")
        if (
            payload.get("success") != 1
            or not isinstance(data, dict)
            or data.get("art_code") != art_code
            or not isinstance(data.get("notice_content"), str)
            or not data["notice_content"].strip()
        ):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return data["notice_content"]

    async def _previous_close(
        self, symbol: str, effective_date: date, deadline: datetime
    ) -> Decimal:
        result = await self.daily_bars(
            DailyBarRequest(
                symbol,
                effective_date - timedelta(days=14),
                effective_date - timedelta(days=1),
                ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
            ),
            deadline,
        )
        eligible = [
            item for item in result.items if item.trading_date < effective_date
        ]
        if not eligible:
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")
        return eligible[-1].close

    @staticmethod
    def _row_date(row: dict[str, Any], key: str) -> date:
        value = row.get(key)
        if not isinstance(value, str):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        try:
            return datetime.fromisoformat(value).date()
        except ValueError as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error

    @staticmethod
    def _optional_decimal(row: dict[str, Any], key: str) -> Decimal:
        value = row.get(key)
        if value in (None, "", "-"):
            return Decimal(0)
        result = _decimal(value)
        if result < 0:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return result

    @staticmethod
    def _required_positive_decimal(row: dict[str, Any], key: str) -> Decimal:
        result = _decimal(row.get(key))
        if result <= 0:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return result

    @staticmethod
    def _validate_action_symbol(row: dict[str, Any], symbol: str) -> None:
        if row.get("SECURITY_CODE") != symbol[:6]:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        secucode = row.get("SECUCODE")
        if secucode is not None and secucode != symbol:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")

    @staticmethod
    def _announcement_has_column(
        announcement: dict[str, Any], expected: str
    ) -> bool:
        columns = announcement.get("columns")
        return isinstance(columns, list) and any(
            isinstance(item, dict) and item.get("column_code") == expected
            for item in columns
        )

    @classmethod
    def _publication_time(cls, announcement: dict[str, Any]) -> datetime:
        parsed = []
        for key in ("display_time", "eiTime"):
            raw = announcement.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            value = raw.strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3}", value):
                value = f"{value[:-4]}.{value[-3:]}"
            try:
                parsed.append(datetime.fromisoformat(value).replace(tzinfo=cls._CHINA))
            except ValueError as error:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        if not parsed:
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")
        return max(parsed).astimezone(UTC)

    @classmethod
    def _validate_publication(
        cls, published_at: datetime, effective_date: date, observed_at: datetime
    ) -> None:
        market_open = datetime.combine(
            effective_date,
            datetime.min.time().replace(hour=9, minute=30),
            tzinfo=cls._CHINA,
        ).astimezone(UTC)
        if published_at >= market_open or published_at > observed_at:
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")

    @staticmethod
    def _number_pattern(value: Decimal) -> str:
        normalized = format(value.normalize(), "f")
        whole, dot, fraction = normalized.partition(".")
        if not dot or not fraction:
            return rf"{re.escape(whole)}(?:\.0+)?"
        return rf"{re.escape(whole)}\.{re.escape(fraction)}0*"

    @classmethod
    def _validate_dividend_content(
        cls,
        content: str,
        *,
        event_date: date,
        effective_date: date,
        cash_per_ten: Decimal,
        bonus_per_ten: Decimal,
        transfer_per_ten: Decimal,
    ) -> None:
        compact = re.sub(r"\s+", "", content)
        cls._require_chinese_date(compact, event_date)
        cls._require_chinese_date(compact, effective_date)
        checks = (
            (
                cash_per_ten,
                rf"每10股(?:派发现金|派)[^\d]{{0,20}}"
                rf"{cls._number_pattern(cash_per_ten)}元",
            ),
            (
                bonus_per_ten,
                rf"每10股(?:送红股|送股|送)"
                rf"{cls._number_pattern(bonus_per_ten)}股",
            ),
            (
                transfer_per_ten,
                rf"每10股转增{cls._number_pattern(transfer_per_ten)}股",
            ),
        )
        if any(
            value > 0 and re.search(pattern, compact) is None
            for value, pattern in checks
        ):
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")

    @classmethod
    def _validate_rights_content(
        cls,
        issue_content: str,
        result_content: str,
        *,
        event_date: date,
        ratio_per_ten: Decimal,
        issue_price: Decimal,
        issue_num: Decimal,
    ) -> None:
        issue = re.sub(r"[\s,，]+", "", issue_content)
        result = re.sub(r"[\s,，]+", "", result_content)
        cls._require_chinese_date(issue, event_date)
        ratio = cls._number_pattern(ratio_per_ten)
        price = cls._number_pattern(issue_price)
        issued = cls._number_pattern(issue_num)
        if (
            re.search(rf"每10股配售{ratio}股", issue) is None
            or re.search(rf"配股价格(?:为|：|:)?{price}元/股", issue) is None
            or re.search(issued, result) is None
        ):
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")

    @staticmethod
    def _require_chinese_date(content: str, expected: date) -> None:
        pattern = (
            rf"{expected.year}年0?{expected.month}月0?{expected.day}日"
        )
        if re.search(pattern, content) is None:
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")

    @staticmethod
    def _dividend_event_type(
        cash: Decimal, bonus: Decimal, transfer: Decimal
    ) -> CorporateActionType:
        components = sum(value > 0 for value in (cash, bonus, transfer))
        if components > 1:
            return CorporateActionType.COMPOSITE
        if cash > 0:
            return CorporateActionType.CASH_DIVIDEND
        return CorporateActionType.BONUS_SHARE

    def _action_record(
        self,
        symbol: str,
        announcement: dict[str, Any],
        event_type: CorporateActionType,
        event_date: date,
        effective_date: date,
        published_at: datetime,
        observed_at: datetime,
        factor: Decimal,
        structured_row: dict[str, Any],
        content: str,
        previous_close: Decimal,
    ) -> CorporateActionRecord:
        art_code = str(announcement["art_code"])
        evidence = {
            "structured_row": structured_row,
            "announcement": announcement,
            "announcement_content": content,
            "previous_close": str(previous_close),
        }
        raw_hash = sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        references = "+".join(
            f"{self.ANNOUNCEMENT_CONTENT_URL}?art_code={code}"
            for code in art_code.split("+")
        )
        return CorporateActionRecord(
            symbol=symbol,
            source_event_id=art_code,
            event_type=event_type,
            event_date=event_date,
            effective_date=effective_date,
            published_at=published_at,
            observed_at=observed_at,
            adjustment_factor=factor,
            source_reference=references,
            raw_payload_hash=raw_hash,
            source=self.code,
        )

    @staticmethod
    def _attach_schema_sample(error: ProviderHttpError, payload: Any) -> None:
        if error.code != "PROVIDER_SCHEMA_INCOMPATIBLE" or error.response_sample:
            return
        error.response_sample = {
            "body_excerpt": json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )[:2048]
        }

    async def probe(
        self, capability: ProviderCapability, deadline: datetime
    ) -> ProbeResult:
        started = monotonic()
        try:
            if capability is ProviderCapability.REALTIME_QUOTE_BATCH:
                await self.realtime_quotes(("600000.SH",), deadline)
            elif capability is ProviderCapability.SECURITY_MASTER:
                payload = await self._json(
                    self.MASTER_PROBE_URL,
                    {"secid": "1.600000", "fields": "f2,f12,f14,f26"},
                    deadline,
                )
                if not isinstance(payload.get("data"), dict):
                    raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
                self.parse_security_master(
                    {"rc": payload.get("rc"), "data": {"diff": [payload["data"]]}},
                    observed_at=datetime.now(UTC),
                )
            elif capability is ProviderCapability.CORPORATE_ACTIONS:
                today = date.today()
                await self.corporate_actions(
                    CorporateActionRequest(
                        "600000.SH", today - timedelta(days=30), today
                    ),
                    deadline,
                )
            else:
                today = date.today()
                await self.daily_bars(
                    DailyBarRequest(
                        "600000.SH", today - timedelta(days=7), today, capability
                    ),
                    deadline,
                )
            healthy, error_code = True, None
        except Exception as error:
            healthy = False
            error_code = getattr(error, "code", "PROVIDER_FAILED")
        return ProbeResult(
            self.code,
            capability,
            healthy,
            datetime.now(UTC),
            int((monotonic() - started) * 1000),
            error_code,
        )

    async def _json(
        self,
        url: str,
        params: dict[str, str],
        deadline: datetime,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("provider client is not configured")
        return await self._client.request_json(
            ProviderHttpRequest(url, params, headers or {}), deadline=deadline
        )

    @staticmethod
    def _rows(payload: Any, key: str) -> list[Any]:
        if not isinstance(payload, dict) or payload.get("rc") != 0:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get(key), list):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return data[key]

    def parse_quotes(
        self, payload: Any, symbols: tuple[str, ...], *, received_at: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        rows = self._rows(payload, "diff")
        parsed: dict[str, RealtimeQuote] = {}
        row_failures: dict[str, ProviderItemFailure] = {}
        required = ("f12", "f2", "f17", "f15", "f16", "f18", "f5", "f6", "f124")
        for row in rows:
            if not isinstance(row, dict) or "f12" not in row:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            symbol = _symbol(str(row["f12"]))
            try:
                if any(key not in row for key in required):
                    raise ValueError("missing quote field")
                timestamp = int(row["f124"])
                quote_time = datetime.fromtimestamp(timestamp, UTC)
                if quote_time.year < 2000 or quote_time > received_at.astimezone(
                    UTC
                ).replace(microsecond=0) + timedelta(days=1):
                    raise ValueError("invalid quote time")
                parsed[symbol] = RealtimeQuote(
                    symbol=symbol,
                    price=_decimal(row["f2"]),
                    open=_decimal(row["f17"]),
                    high=_decimal(row["f15"]),
                    low=_decimal(row["f16"]),
                    previous_close=_decimal(row["f18"]),
                    volume=int(row["f5"]),
                    amount=_decimal(row["f6"]),
                    quote_time=quote_time,
                    received_at=received_at,
                    source=self.code,
                )
            except (ProviderHttpError, ValueError, TypeError, OSError):
                row_failures[symbol] = ProviderItemFailure(
                    symbol,
                    "PROVIDER_ITEM_INVALID",
                    "该股票行情字段无效",
                    self.code,
                )
        items = tuple(parsed[symbol] for symbol in symbols if symbol in parsed)
        failures = tuple(
            row_failures.get(symbol)
            or ProviderItemFailure(
                symbol, "PROVIDER_ITEM_MISSING", "上游未返回该股票", self.code
            )
            for symbol in symbols
            if symbol not in parsed
        )
        return ProviderBatchResult(items, failures)

    def parse_bars(
        self, payload: Any, *, request: DailyBarRequest
    ) -> ProviderBatchResult[DailyBar]:
        rows = self._rows(payload, "klines")
        data = payload["data"]
        if data.get("code") != request.symbol[:6]:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        items: list[DailyBar] = []
        seen_dates: set[date] = set()
        previous_date: date | None = None
        try:
            for row in rows:
                fields = row.split(",")
                if len(fields) != 7:
                    raise ValueError
                trading_date = date.fromisoformat(fields[0])
                if (
                    trading_date < request.start
                    or trading_date > request.end
                    or trading_date in seen_dates
                    or (previous_date is not None and trading_date <= previous_date)
                ):
                    raise ValueError
                seen_dates.add(trading_date)
                previous_date = trading_date
                items.append(
                    DailyBar(
                        request.symbol,
                        trading_date,
                        _decimal(fields[1]),
                        _decimal(fields[3]),
                        _decimal(fields[4]),
                        _decimal(fields[2]),
                        int(fields[5]),
                        _decimal(fields[6]),
                        self.code,
                        request.capability,
                    )
                )
        except (AttributeError, ValueError, TypeError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        expected_weekdays: set[date] = set()
        cursor = request.start
        while cursor <= request.end:
            if cursor.weekday() < 5:
                expected_weekdays.add(cursor)
            cursor += timedelta(days=1)
        missing_dates = sorted(expected_weekdays - seen_dates)
        failures = ()
        if missing_dates:
            failures = (
                ProviderItemFailure(
                    request.symbol,
                    "PROVIDER_TRADING_DATES_MISSING",
                    "请求期内缺少交易日数据；需结合停牌或交易日历确认",
                    self.code,
                ),
            )
        return ProviderBatchResult(tuple(items), failures)

    def parse_security_master(
        self, payload: Any, *, observed_at: datetime
    ) -> tuple[SecurityMasterRecord, ...]:
        records = []
        for row in self._rows(payload, "diff"):
            if not isinstance(row, dict) or not {"f12", "f14"} <= row.keys():
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            symbol = _symbol(str(row["f12"]))
            listed_on = None
            delisted_on = None
            listing_value = row.get("f26")
            if listing_value not in (None, "", "-"):
                try:
                    listed_on = datetime.strptime(str(listing_value), "%Y%m%d").date()
                except ValueError as error:
                    raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
            delisting_value = row.get("f80")
            if delisting_value not in (None, "", "-"):
                try:
                    delisted_on = datetime.strptime(
                        str(delisting_value), "%Y%m%d"
                    ).date()
                except ValueError as error:
                    raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
            if delisted_on is not None and delisted_on <= observed_at.date():
                listed = False
            elif listed_on is not None:
                listed = listed_on <= observed_at.date()
            else:
                listed = None
            price_value = row.get("f2")
            if listed is True and price_value == "-":
                suspended = True
            elif listed is True and price_value not in (None, "", "-"):
                try:
                    _decimal(price_value)
                    suspended = False
                except ProviderHttpError:
                    suspended = None
            else:
                suspended = None
            records.append(
                SecurityMasterRecord(
                    symbol,
                    str(row["f14"]),
                    symbol[-2:],
                    "STOCK",
                    listed_on,
                    delisted_on,
                    listed,
                    str(row["f14"]).upper().startswith(("ST", "*ST")),
                    suspended,
                    self.code,
                    observed_at,
                )
            )
        return tuple(records)
