from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic

from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyBarRequest,
    ProbeResult,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
    RealtimeQuote,
)
from long_invest.modules.providers.http_client import (
    ProviderHttpClient,
    ProviderHttpRequest,
)
from long_invest.modules.providers.retry import ProviderHttpError

LINE = re.compile(r'^var hq_str_(sh|sz|bj)(\d{6})="(.*)";$')
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


class SinaRealtimeProvider:
    code = ProviderCode.SINA
    capabilities = frozenset({ProviderCapability.REALTIME_QUOTE_BATCH})
    REALTIME_URL = "https://hq.sinajs.cn/list="
    REFERER = "https://finance.sina.com.cn/"

    def __init__(self, client: ProviderHttpClient | None) -> None:
        self._client = client

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        codes = ",".join(symbol[-2:].lower() + symbol[:6] for symbol in symbols)
        if self._client is None:
            raise RuntimeError("provider client is not configured")
        text = await self._client.request_text(
            ProviderHttpRequest(
                self.REALTIME_URL + codes,
                headers={"Referer": self.REFERER},
            ),
            deadline=deadline,
            encoding="gb18030",
        )
        return self.parse_quotes(text, symbols, received_at=datetime.now(UTC))

    async def security_master(self, deadline: datetime):
        del deadline
        raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")

    async def daily_bars(
        self, request: DailyBarRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]:
        del request, deadline
        raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")

    async def probe(
        self, capability: ProviderCapability, deadline: datetime
    ) -> ProbeResult:
        started = monotonic()
        if capability is not ProviderCapability.REALTIME_QUOTE_BATCH:
            return ProbeResult(
                self.code,
                capability,
                False,
                datetime.now(UTC),
                0,
                "PROVIDER_CAPABILITY_UNSUPPORTED",
            )
        try:
            await self.realtime_quotes(("600000.SH",), deadline)
            healthy, error_code = True, None
        except Exception as error:
            healthy, error_code = False, getattr(error, "code", "PROVIDER_FAILED")
        return ProbeResult(
            self.code,
            capability,
            healthy,
            datetime.now(UTC),
            int((monotonic() - started) * 1000),
            error_code,
        )

    def parse_quotes(
        self, text: str, symbols: tuple[str, ...], *, received_at: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        if len(text.encode("utf-8")) > 256_000 or any(
            marker in text.lower()
            for marker in ("<html", "captcha", "validatecode", "error")
        ):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        parsed: dict[str, RealtimeQuote] = {}
        row_failures: dict[str, ProviderItemFailure] = {}
        for raw_line in filter(None, (line.strip() for line in text.splitlines())):
            match = LINE.fullmatch(raw_line)
            if not match:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            market, code, content = match.groups()
            symbol = f"{code}.{market.upper()}"
            if not content:
                continue
            fields = content.split(",")
            try:
                if len(fields) < 32:
                    raise ValueError("missing quote fields")
                quote_time = datetime.fromisoformat(
                    f"{fields[30]}T{fields[31]}"
                ).replace(tzinfo=CHINA_TIMEZONE)
                values = [Decimal(fields[index]) for index in (1, 2, 3, 4, 5, 8, 9)]
                parsed[symbol] = RealtimeQuote(
                    symbol,
                    values[2],
                    values[0],
                    values[3],
                    values[4],
                    values[1],
                    int(values[5]),
                    values[6],
                    quote_time,
                    received_at,
                    self.code,
                )
            except (ValueError, InvalidOperation):
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
