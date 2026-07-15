from __future__ import annotations

from datetime import datetime

from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyBarRequest,
    MarketDataProvider,
    ProviderBatchResult,
    ProviderCode,
    ProviderItemFailure,
    RealtimeQuote,
)


class ProviderRouter:
    def __init__(self, eastmoney: MarketDataProvider, sina: MarketDataProvider) -> None:
        self._eastmoney = eastmoney
        self._sina = sina

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        primary = await self._eastmoney.realtime_quotes(symbols, deadline)
        primary_by_symbol = {item.symbol: item for item in primary.items}
        if primary.batch_error_code or not primary.items:
            fallback_symbols = symbols
            primary_by_symbol = {}
        else:
            fallback_symbols = tuple(
                symbol for symbol in symbols if symbol not in primary_by_symbol
            )
        fallback = ProviderBatchResult[RealtimeQuote]()
        if fallback_symbols:
            fallback = await self._sina.realtime_quotes(fallback_symbols, deadline)
        fallback_by_symbol = {item.symbol: item for item in fallback.items}
        items = tuple(
            primary_by_symbol.get(symbol) or fallback_by_symbol[symbol]
            for symbol in symbols
            if symbol in primary_by_symbol or symbol in fallback_by_symbol
        )
        failure_by_symbol = {
            failure.symbol: failure
            for failure in (*primary.failures, *fallback.failures)
        }
        failures = tuple(
            failure_by_symbol.get(symbol)
            or ProviderItemFailure(
                symbol,
                "PROVIDER_ITEM_MISSING",
                "所有可用来源均未返回该股票",
                ProviderCode.SINA,
            )
            for symbol in symbols
            if symbol not in primary_by_symbol and symbol not in fallback_by_symbol
        )
        batch_error = fallback.batch_error_code if not items else None
        return ProviderBatchResult(items, failures, batch_error)

    async def daily_bars(
        self, request: DailyBarRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]:
        return await self._eastmoney.daily_bars(request, deadline)

    async def security_master(self, deadline: datetime):
        return await self._eastmoney.security_master(deadline)
