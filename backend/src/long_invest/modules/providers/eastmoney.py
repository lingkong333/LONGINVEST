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
                "fields": "f12,f14,f26",
                "pz": "10000",
            },
            deadline,
        )
        return self.parse_security_master(
            payload, observed_at=datetime.now(UTC)
        )

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
        return self.parse_quotes(
            payload, symbols, received_at=datetime.now(UTC)
        )

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
        return self.parse_bars(
            payload, symbol=request.symbol, capability=request.capability
        )

    async def probe(
        self, capability: ProviderCapability, deadline: datetime
    ) -> ProbeResult:
        started = monotonic()
        try:
            if capability is ProviderCapability.REALTIME_QUOTE_BATCH:
                await self.realtime_quotes(("600000.SH",), deadline)
            elif capability is ProviderCapability.SECURITY_MASTER:
                await self.security_master(deadline)
            else:
                await self.daily_bars(
                    DailyBarRequest(
                        "600000.SH", date.today(), date.today(), capability
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
        required = ("f12", "f2", "f17", "f15", "f16", "f18", "f5", "f6", "f124")
        for row in rows:
            if not isinstance(row, dict) or any(key not in row for key in required):
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            symbol = _symbol(str(row["f12"]))
            try:
                timestamp = int(row["f124"])
                quote_time = datetime.fromtimestamp(timestamp, UTC)
            except (ValueError, TypeError, OSError) as error:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
            if quote_time.year < 2000 or quote_time > received_at.astimezone(
                UTC
            ).replace(microsecond=0) + timedelta(days=1):
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            try:
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
            except (ValueError, TypeError) as error:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        items = tuple(parsed[symbol] for symbol in symbols if symbol in parsed)
        failures = tuple(
            ProviderItemFailure(
                symbol, "PROVIDER_ITEM_MISSING", "上游未返回该股票", self.code
            )
            for symbol in symbols
            if symbol not in parsed
        )
        return ProviderBatchResult(items, failures)

    def parse_bars(
        self, payload: Any, *, symbol: str, capability: ProviderCapability
    ) -> ProviderBatchResult[DailyBar]:
        rows = self._rows(payload, "klines")
        items: list[DailyBar] = []
        try:
            for row in rows:
                fields = row.split(",")
                if len(fields) != 7:
                    raise ValueError
                items.append(
                    DailyBar(
                        symbol,
                        date.fromisoformat(fields[0]),
                        _decimal(fields[1]),
                        _decimal(fields[3]),
                        _decimal(fields[4]),
                        _decimal(fields[2]),
                        int(fields[5]),
                        _decimal(fields[6]),
                        self.code,
                        capability,
                    )
                )
        except (AttributeError, ValueError, TypeError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        return ProviderBatchResult(tuple(items))

    def parse_security_master(
        self, payload: Any, *, observed_at: datetime
    ) -> tuple[SecurityMasterRecord, ...]:
        records = []
        for row in self._rows(payload, "diff"):
            if not isinstance(row, dict) or not {"f12", "f14"} <= row.keys():
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            symbol = _symbol(str(row["f12"]))
            records.append(
                SecurityMasterRecord(
                    symbol,
                    str(row["f14"]),
                    symbol[-2:],
                    "STOCK",
                    None,
                    None,
                    True,
                    str(row["f14"]).upper().startswith("ST"),
                    False,
                    self.code,
                    observed_at,
                )
            )
        return tuple(records)
