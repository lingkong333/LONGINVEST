from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic

from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyCollectionMode,
    DailyCollectionPlan,
    MarketDailyGroupRequest,
    ProbeResult,
    ProviderAdapterCode,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
    ProviderSourceIdentity,
    RealtimeQuote,
)
from long_invest.modules.providers.http_client import (
    ProviderHttpClient,
    ProviderHttpRequest,
)
from long_invest.modules.providers.retry import ProviderHttpError

LINE = re.compile(r'^v_(sh|sz|bj)(\d{6})="(.*)";$')
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


class TencentRealtimeProvider:
    code = ProviderCode.TENCENT
    capabilities = frozenset(
        {
            ProviderCapability.REALTIME_QUOTE_BATCH,
            ProviderCapability.DAILY_BAR_UNADJUSTED,
        }
    )
    REALTIME_URL = "https://qt.gtimg.cn/q="
    REFERER = "https://gu.qq.com/"

    def __init__(self, client: ProviderHttpClient) -> None:
        self._client = client

    def source_identity(self, capability: ProviderCapability) -> ProviderSourceIdentity:
        return ProviderSourceIdentity(
            adapter=ProviderAdapterCode.HTTPX,
            upstream=self.code,
            interface=self.REALTIME_URL,
            capability=capability,
            algorithm_version="raw-v1",
        )

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        codes = ",".join(symbol[-2:].lower() + symbol[:6] for symbol in symbols)
        text = await self._client.request_text(
            ProviderHttpRequest(
                self.REALTIME_URL + codes,
                headers={"Accept": "*/*", "Referer": self.REFERER},
            ),
            deadline=deadline,
            encoding="gb18030",
            allowed_content_types=frozenset({"text/plain", "text/html"}),
        )
        return self.parse_quotes(text, symbols, received_at=datetime.now(UTC))

    @classmethod
    def parse_quotes(
        cls, text: str, symbols: tuple[str, ...], *, received_at: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        if len(text.encode("utf-8")) > 256_000 or "<html" in text.lower():
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        parsed: dict[str, RealtimeQuote] = {}
        failures: dict[str, ProviderItemFailure] = {}
        for raw_line in filter(None, (line.strip() for line in text.splitlines())):
            match = LINE.fullmatch(raw_line)
            if match is None:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            market, code, content = match.groups()
            symbol = f"{code}.{market.upper()}"
            try:
                fields = content.split("~")
                if len(fields) < 38:
                    raise ValueError("missing quote fields")
                quote_time = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
                    tzinfo=CHINA_TIMEZONE
                )
                trade_summary = fields[35].split("/")
                amount = (
                    Decimal(trade_summary[2])
                    if len(trade_summary) == 3 and trade_summary[2]
                    else Decimal(fields[37]) * Decimal("10000")
                )
                parsed[symbol] = RealtimeQuote(
                    symbol=symbol,
                    price=Decimal(fields[3]),
                    open=Decimal(fields[5]),
                    high=Decimal(fields[33]),
                    low=Decimal(fields[34]),
                    previous_close=Decimal(fields[4]),
                    volume=int(fields[6]) * 100,
                    amount=amount,
                    quote_time=quote_time,
                    received_at=received_at,
                    source=cls.code,
                )
            except (InvalidOperation, ValueError):
                failures[symbol] = ProviderItemFailure(
                    symbol,
                    "PROVIDER_ITEM_INVALID",
                    "该股票行情字段无效",
                    cls.code,
                )
        items = tuple(parsed[symbol] for symbol in symbols if symbol in parsed)
        missing = tuple(
            failures.get(symbol)
            or ProviderItemFailure(
                symbol, "PROVIDER_ITEM_MISSING", "上游未返回该股票", cls.code
            )
            for symbol in symbols
            if symbol not in parsed
        )
        return ProviderBatchResult(items, missing)

    async def probe(
        self, capability: ProviderCapability, deadline: datetime
    ) -> ProbeResult:
        started = monotonic()
        try:
            if capability not in self.capabilities:
                raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")
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

    async def security_master(self, deadline):
        del deadline
        raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")

    async def daily_bars(self, request, deadline):
        del request, deadline
        raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")

    def daily_collection_plan(self, total_symbols: int) -> DailyCollectionPlan:
        return DailyCollectionPlan(
            self.code,
            DailyCollectionMode.BATCHED_SYMBOLS,
            total_symbols,
            100,
            0.5,
        )

    async def market_daily_bars(
        self, request: MarketDailyGroupRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]:
        quotes = await self.realtime_quotes(request.symbols, deadline)
        items: list[DailyBar] = []
        failures = list(quotes.failures)
        for quote in quotes.items:
            quote_date = quote.quote_time.astimezone(CHINA_TIMEZONE).date()
            if quote_date != request.trading_date:
                failures.append(
                    ProviderItemFailure(
                        quote.symbol,
                        "PROVIDER_ITEM_STALE",
                        "行情日期与目标交易日不一致",
                        self.code,
                    )
                )
                continue
            items.append(
                DailyBar(
                    symbol=quote.symbol,
                    trading_date=request.trading_date,
                    open=quote.open,
                    high=quote.high,
                    low=quote.low,
                    close=quote.price,
                    volume=quote.volume,
                    amount=quote.amount,
                    source=self.code,
                    capability=ProviderCapability.DAILY_BAR_UNADJUSTED,
                    collected_at=quote.received_at,
                )
            )
        return ProviderBatchResult(
            tuple(items), tuple(failures), quotes.batch_error_code
        )

    async def corporate_actions(self, request, deadline):
        del request, deadline
        raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")
