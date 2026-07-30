from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

from long_invest.modules.providers.contracts import (
    CorporateActionRecord,
    CorporateActionRequest,
    DailyBar,
    DailyBarRequest,
    DailyCollectionPlan,
    MarketDailyGroupRequest,
    MarketDataProvider,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
    ProviderMissingRange,
    ProviderSourceIdentity,
    RealtimeQuote,
)
from long_invest.modules.providers.resilience import (
    InMemoryProviderRuntimeState,
    ProviderConfigurationPort,
    ProviderInvocationPipeline,
    ProviderRoutePlan,
    ProviderRouteSetting,
    ProviderRuntimeObserverPort,
    ProviderRuntimeStatePort,
    StaticProviderConfiguration,
)


class ProviderRouter:
    def __init__(
        self,
        eastmoney: MarketDataProvider | None = None,
        sina: MarketDataProvider | None = None,
        *,
        providers: dict[ProviderCode, MarketDataProvider] | None = None,
        config: ProviderConfigurationPort | None = None,
        runtime: ProviderRuntimeStatePort | None = None,
        observer: ProviderRuntimeObserverPort | None = None,
        conflict_observer: ProviderConflictObserverPort | None = None,
    ) -> None:
        registered = dict(providers or {})
        if eastmoney is not None:
            registered[ProviderCode.EASTMONEY] = eastmoney
        if sina is not None:
            registered[ProviderCode.SINA] = sina
        if not registered:
            raise ValueError("provider router requires at least one provider")
        self._providers = registered
        self._config = config or StaticProviderConfiguration()
        self._runtime = runtime or InMemoryProviderRuntimeState()
        if observer is None and hasattr(self._config, "record_outcome"):
            observer = self._config  # type: ignore[assignment]
        self._pipeline = ProviderInvocationPipeline(self._runtime, observer)
        self._conflict_observer = conflict_observer

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        plan = await self._route_plan(ProviderCapability.REALTIME_QUOTE_BATCH)
        routes = plan.routes
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
                chosen[item.symbol] = self._with_identity(item, provider, setting)
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
                routes[-1].provider if routes else ProviderCode.EASTMONEY,
            )
            for symbol in missing
        )
        items = tuple(chosen[symbol] for symbol in symbols if symbol in chosen)
        batch_error = None
        if not items and attempted:
            batch_error = last_batch_error
        if not attempted:
            batch_error = (
                "PROVIDER_FIXED_SOURCE_UNAVAILABLE"
                if plan.fixed_provider is not None
                else "PROVIDER_UNAVAILABLE"
            )
        return ProviderBatchResult(items, final_failures, batch_error)

    async def daily_bars(
        self,
        request: DailyBarRequest,
        deadline: datetime,
        *,
        concurrency: int | None = None,
    ) -> ProviderBatchResult[DailyBar]:
        if concurrency is not None and (
            concurrency < 1
            or request.capability
            not in {
                ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
                ProviderCapability.HISTORICAL_DAILY_QFQ,
            }
        ):
            raise ValueError("invalid historical provider concurrency")
        return await self._daily(
            request.capability,
            deadline,
            request,
            concurrency_override=concurrency,
        )

    async def daily_collection_plan(self, total_symbols: int) -> DailyCollectionPlan:
        route_plan = await self._route_plan(ProviderCapability.DAILY_BAR_UNADJUSTED)
        for setting in route_plan.routes:
            provider = self._providers.get(setting.provider)
            if setting.enabled and provider is not None:
                return provider.daily_collection_plan(total_symbols)
        code = (
            "PROVIDER_FIXED_SOURCE_UNAVAILABLE"
            if route_plan.fixed_provider is not None
            else "PROVIDER_UNAVAILABLE"
        )
        raise ProviderRoutingError(code)

    async def market_daily_bars(
        self,
        plan: DailyCollectionPlan,
        request: MarketDailyGroupRequest,
        deadline: datetime,
    ) -> ProviderBatchResult[DailyBar]:
        route_plan = await self._route_plan(ProviderCapability.DAILY_BAR_UNADJUSTED)
        setting = next(
            (
                item
                for item in route_plan.routes
                if item.provider is plan.provider and item.enabled
            ),
            None,
        )
        provider = self._providers.get(plan.provider)
        if setting is None or provider is None:
            raise ProviderRoutingError("PROVIDER_UNAVAILABLE")
        result = await self._pipeline.call(
            setting,
            lambda: provider.market_daily_bars(request, deadline),
            deadline=deadline,
        )
        return self._enrich_result(result, provider, setting)

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
        provider = self._providers.get(setting.provider)
        if provider is None:
            raise ProviderRoutingError("PROVIDER_FIXED_SOURCE_UNAVAILABLE")
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
        routes = (
            await self._route_plan(ProviderCapability.REALTIME_QUOTE_BATCH)
        ).routes
        setting = next(
            (route for route in routes if route.provider is provider_code),
            None,
        )
        provider = self._providers.get(provider_code)
        if setting is None or provider is None:
            raise ProviderRoutingError("PROVIDER_UNAVAILABLE")
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
        *,
        concurrency_override: int | None = None,
    ):
        plan = await self._route_plan(capability)
        routes = plan.routes
        last_error: Exception | None = None
        for setting in routes:
            if not setting.enabled:
                continue
            if concurrency_override is not None:
                setting = replace(setting, concurrency=concurrency_override)
            provider = self._providers.get(setting.provider)
            if provider is None:
                continue
            try:
                result = await self._pipeline.call(
                    setting,
                    lambda p=provider: operation(p),
                    deadline=deadline,
                )
                if isinstance(result, ProviderBatchResult) and result.batch_error_code:
                    last_error = ProviderRoutingError(result.batch_error_code)
                    if setting.auto_switch and plan.fixed_provider is None:
                        continue
                return self._enrich_result(result, provider, setting)
            except Exception as error:
                last_error = error
                if not setting.auto_switch or plan.fixed_provider is not None:
                    raise
        if last_error is not None:
            raise last_error
        code = (
            "PROVIDER_FIXED_SOURCE_UNAVAILABLE"
            if plan.fixed_provider is not None
            else "PROVIDER_UNAVAILABLE"
        )
        raise ProviderRoutingError(code)

    async def _daily(
        self,
        capability: ProviderCapability,
        deadline: datetime,
        request: DailyBarRequest,
        *,
        concurrency_override: int | None,
    ) -> ProviderBatchResult[DailyBar]:
        plan = await self._route_plan(capability)
        chosen: dict[object, DailyBar] = {}
        conflicts: dict[object, ProviderItemFailure] = {}
        last_error: str | None = None
        attempted = False
        pending_requests = (request,)
        for setting in plan.routes:
            provider = self._providers.get(setting.provider)
            if not setting.enabled or provider is None:
                continue
            if concurrency_override is not None:
                setting = replace(setting, concurrency=concurrency_override)
            attempted = True
            try:
                results = []
                for pending in pending_requests:
                    results.append(
                        await self._pipeline.call(
                            setting,
                            lambda p=provider, r=pending: p.daily_bars(r, deadline),
                            deadline=deadline,
                            switched=bool(chosen),
                        )
                    )
            except Exception as error:
                last_error = getattr(error, "code", "PROVIDER_FAILED")
                if not setting.auto_switch or plan.fixed_provider is not None:
                    break
                continue
            last_error = next(
                (item.batch_error_code for item in results if item.batch_error_code),
                None,
            )
            for raw in (item for result in results for item in result.items):
                item = self._with_identity(raw, provider, setting)
                key = item.trading_date
                previous = chosen.get(key)
                if previous is not None and self._bar_values(
                    previous
                ) != self._bar_values(item):
                    if self._conflict_observer is not None:
                        await self._conflict_observer.record_daily_conflict(
                            previous, item
                        )
                    chosen.pop(key, None)
                    conflicts[key] = ProviderItemFailure(
                        item.symbol,
                        "PROVIDER_DATA_CONFLICT",
                        f"{item.trading_date.isoformat()} 数据冲突，已转人工复核",
                        item.source,
                    )
                elif key not in conflicts:
                    chosen[key] = item
            missing_ranges: tuple[ProviderMissingRange, ...] = tuple(
                missing for result in results for missing in result.missing_ranges
            )
            needs_fallback = bool(
                last_error
                or any(result.failures for result in results)
                or missing_ranges
            )
            if missing_ranges:
                pending_requests = tuple(
                    DailyBarRequest(
                        symbol=missing.symbol,
                        start=missing.start,
                        end=missing.end,
                        capability=capability,
                    )
                    for missing in missing_ranges
                )
            else:
                pending_requests = (request,)
            if (
                not needs_fallback
                or not setting.auto_switch
                or plan.fixed_provider is not None
            ):
                break
        items = tuple(chosen[key] for key in sorted(chosen))
        failures = tuple(conflicts[key] for key in sorted(conflicts))
        if not attempted:
            last_error = (
                "PROVIDER_FIXED_SOURCE_UNAVAILABLE"
                if plan.fixed_provider is not None
                else "PROVIDER_UNAVAILABLE"
            )
        return ProviderBatchResult(items, failures, last_error if not items else None)

    async def _route_plan(self, capability: ProviderCapability) -> ProviderRoutePlan:
        factory = getattr(self._config, "route_plan", None)
        if factory is not None:
            return await factory(capability)
        return ProviderRoutePlan(capability, await self._config.routes(capability))

    def _enrich_result(
        self, result: Any, provider: MarketDataProvider, setting: ProviderRouteSetting
    ):
        if getattr(provider, "source_identity", None) is None:
            return result
        if isinstance(result, ProviderBatchResult):
            return ProviderBatchResult(
                tuple(
                    self._with_identity(item, provider, setting)
                    for item in result.items
                ),
                result.failures,
                result.batch_error_code,
                result.missing_ranges,
            )
        if isinstance(result, tuple):
            return tuple(
                self._with_identity(item, provider, setting) for item in result
            )
        return result

    @staticmethod
    def _with_identity(
        item: Any, provider: MarketDataProvider, setting: ProviderRouteSetting
    ):
        identity_factory = getattr(provider, "source_identity", None)
        if identity_factory is None:
            return item
        identity: ProviderSourceIdentity = identity_factory(setting.capability)
        changes: dict[str, Any] = {"source_identity": identity}
        if isinstance(item, DailyBar):
            changes["collected_at"] = item.collected_at or datetime.now(UTC)
        return replace(item, **changes)

    @staticmethod
    def _bar_values(item: DailyBar) -> tuple[Any, ...]:
        return (item.open, item.high, item.low, item.close, item.volume, item.amount)


class ProviderRoutingError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderConflictObserverPort(Protocol):
    async def record_daily_conflict(
        self, primary: DailyBar, candidate: DailyBar
    ) -> None: ...
