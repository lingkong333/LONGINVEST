from __future__ import annotations

from datetime import datetime
from typing import Any

from long_invest.modules.providers.contracts import (
    CorporateActionRecord,
    CorporateActionRequest,
    DailyBar,
    DailyBarRequest,
    MarketDataProvider,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
    RealtimeQuote,
)
from long_invest.modules.providers.resilience import (
    InMemoryProviderRuntimeState,
    ProviderConfigurationPort,
    ProviderInvocationPipeline,
    ProviderRouteSetting,
    ProviderRuntimeObserverPort,
    ProviderRuntimeStatePort,
    StaticProviderConfiguration,
)


class ProviderRouter:
    def __init__(
        self,
        eastmoney: MarketDataProvider,
        sina: MarketDataProvider,
        *,
        config: ProviderConfigurationPort | None = None,
        runtime: ProviderRuntimeStatePort | None = None,
        observer: ProviderRuntimeObserverPort | None = None,
    ) -> None:
        self._providers = {
            ProviderCode.EASTMONEY: eastmoney,
            ProviderCode.SINA: sina,
        }
        self._config = config or StaticProviderConfiguration()
        self._runtime = runtime or InMemoryProviderRuntimeState()
        if observer is None and hasattr(self._config, "record_outcome"):
            observer = self._config  # type: ignore[assignment]
        self._pipeline = ProviderInvocationPipeline(self._runtime, observer)

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        routes = await self._config.routes(ProviderCapability.REALTIME_QUOTE_BATCH)
        chosen: dict[str, RealtimeQuote] = {}
        failures: dict[str, ProviderItemFailure] = {}
        last_batch_error: str | None = None
        attempted = False
        for setting in routes:
            if not setting.enabled:
                continue
            requested = tuple(symbol for symbol in symbols if symbol not in chosen)
            if not requested:
                break
            provider = self._providers.get(setting.provider)
            if provider is None:
                continue
            switched = attempted
            attempted = True
            try:
                result = await self._pipeline.call(
                    setting,
                    lambda p=provider, s=requested: p.realtime_quotes(s, deadline),
                    deadline=deadline,
                    switched=switched,
                )
            except Exception as error:
                last_batch_error = getattr(error, "code", "PROVIDER_FAILED")
                if not setting.auto_switch:
                    break
                continue
            last_batch_error = result.batch_error_code
            for item in result.items:
                chosen[item.symbol] = item
                failures.pop(item.symbol, None)
            for failure in result.failures:
                failures[failure.symbol] = failure
            if result.batch_error_code and not setting.auto_switch:
                break
            if not result.batch_error_code and not result.failures:
                break
            if not setting.auto_switch:
                break
        missing = tuple(symbol for symbol in symbols if symbol not in chosen)
        final_failures = tuple(
            failures.get(symbol)
            or ProviderItemFailure(
                symbol,
                "PROVIDER_ITEM_MISSING",
                "所有可用来源均未返回该股票",
                ProviderCode.SINA,
            )
            for symbol in missing
        )
        items = tuple(chosen[symbol] for symbol in symbols if symbol in chosen)
        batch_error = None
        if not items and attempted:
            batch_error = last_batch_error
        if not attempted:
            batch_error = "PROVIDER_UNAVAILABLE"
        return ProviderBatchResult(items, final_failures, batch_error)

    async def daily_bars(
        self, request: DailyBarRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]:
        return await self._single(
            request.capability,
            deadline,
            lambda provider: provider.daily_bars(request, deadline),
        )

    async def corporate_actions(
        self, request: CorporateActionRequest, deadline: datetime
    ) -> ProviderBatchResult[CorporateActionRecord]:
        return await self._single(
            ProviderCapability.CORPORATE_ACTIONS,
            deadline,
            lambda provider: provider.corporate_actions(request, deadline),
        )

    async def security_master(self, deadline: datetime):
        return await self._single(
            ProviderCapability.SECURITY_MASTER,
            deadline,
            lambda provider: provider.security_master(deadline),
        )

    async def probe(
        self,
        setting: ProviderRouteSetting,
        deadline: datetime,
        *,
        force_half_open: bool = False,
    ):
        provider = self._providers[setting.provider]
        if force_half_open:
            await self._runtime.force_half_open(setting)
        return await self._pipeline.call(
            setting,
            lambda: provider.probe(setting.capability, deadline),
            deadline=deadline,
            probe=force_half_open,
            observe=False,
        )

    async def realtime_quotes_from(
        self,
        provider_code: ProviderCode,
        symbols: tuple[str, ...],
        deadline: datetime,
    ) -> ProviderBatchResult[RealtimeQuote]:
        routes = await self._config.routes(ProviderCapability.REALTIME_QUOTE_BATCH)
        setting = next(
            (route for route in routes if route.provider is provider_code),
            None,
        )
        provider = self._providers.get(provider_code)
        if setting is None or provider is None:
            raise RuntimeError("PROVIDER_UNAVAILABLE")
        return await self._pipeline.call(
            setting,
            lambda: provider.realtime_quotes(symbols, deadline),
            deadline=deadline,
        )

    async def diagnostic_quotes(
        self,
        provider_code: ProviderCode,
        symbols: tuple[str, ...],
        deadline: datetime,
    ) -> ProviderBatchResult[RealtimeQuote]:
        return await self.realtime_quotes_from(provider_code, symbols, deadline)

    async def _single(
        self,
        capability: ProviderCapability,
        deadline: datetime,
        operation: Any,
    ):
        routes = await self._config.routes(capability)
        for setting in routes:
            if not setting.enabled:
                continue
            provider = self._providers.get(setting.provider)
            if provider is None:
                continue
            return await self._pipeline.call(
                setting,
                lambda p=provider: operation(p),
                deadline=deadline,
            )
        raise RuntimeError("PROVIDER_UNAVAILABLE")
