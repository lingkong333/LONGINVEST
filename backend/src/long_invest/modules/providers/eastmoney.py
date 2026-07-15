from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

from long_invest.modules.providers.contracts import (
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
        return self.parse_security_master(payload, observed_at=datetime.now(UTC))

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
        return self.parse_quotes(payload, symbols, received_at=datetime.now(UTC))

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
                "klt": "101",
                "fqt": fqt,
                "beg": request.start.strftime("%Y%m%d"),
                "end": request.end.strftime("%Y%m%d"),
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            },
            deadline,
        )
        return self.parse_bars(payload, request=request)

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
        self, url: str, params: dict[str, str], deadline: datetime
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("provider client is not configured")
        return await self._client.request_json(
            ProviderHttpRequest(url, params), deadline=deadline
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
