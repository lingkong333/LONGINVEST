from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from long_invest.modules.providers.budget import (
    enter_request_context,
    exit_request_context,
)
from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    DISABLED = "DISABLED"


@dataclass(slots=True)
class _Circuit:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: datetime | None = None
    cooldown_index: int = 0
    probe_in_flight: bool = False
    probe_token: str | None = None


class CircuitBreaker:
    _cooldowns = (60, 180, 300)

    def __init__(self) -> None:
        self._circuits: dict[tuple[ProviderCode, ProviderCapability], _Circuit] = {}

    def _get(self, provider: ProviderCode, capability: ProviderCapability) -> _Circuit:
        return self._circuits.setdefault((provider, capability), _Circuit())

    def state(
        self, provider: ProviderCode, capability: ProviderCapability, *, now: datetime
    ) -> CircuitState:
        item = self._get(provider, capability)
        if item.state is CircuitState.OPEN and item.opened_at is not None:
            elapsed = (now - item.opened_at).total_seconds()
            if elapsed >= self._cooldowns[item.cooldown_index]:
                item.state = CircuitState.HALF_OPEN
                item.probe_in_flight = False
        return item.state

    def allow(
        self, provider: ProviderCode, capability: ProviderCapability, *, now: datetime
    ) -> bool:
        item = self._get(provider, capability)
        state = self.state(provider, capability, now=now)
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN and not item.probe_in_flight:
            item.probe_in_flight = True
            item.probe_token = uuid4().hex
            return True
        return False

    def record_failure(
        self, provider: ProviderCode, capability: ProviderCapability, *, now: datetime
    ) -> None:
        item = self._get(provider, capability)
        if item.state is CircuitState.DISABLED:
            return
        if item.state is CircuitState.HALF_OPEN:
            item.cooldown_index = min(item.cooldown_index + 1, len(self._cooldowns) - 1)
            item.state = CircuitState.OPEN
            item.opened_at = now
            item.probe_in_flight = False
            item.probe_token = None
            return
        item.consecutive_failures += 1
        if item.consecutive_failures >= 3:
            item.state = CircuitState.OPEN
            item.opened_at = now
            item.probe_in_flight = False
            item.probe_token = None

    def record_success(
        self, provider: ProviderCode, capability: ProviderCapability, *, now: datetime
    ) -> None:
        del now
        item = self._get(provider, capability)
        item.state = CircuitState.CLOSED
        item.consecutive_failures = 0
        item.opened_at = None
        item.cooldown_index = 0
        item.probe_in_flight = False
        item.probe_token = None

    def disable(self, provider: ProviderCode, capability: ProviderCapability) -> None:
        item = self._get(provider, capability)
        item.state = CircuitState.DISABLED
        item.probe_in_flight = False
        item.probe_token = None

    def enable_for_probe(
        self, provider: ProviderCode, capability: ProviderCapability
    ) -> None:
        item = self._get(provider, capability)
        item.state = CircuitState.HALF_OPEN
        item.probe_in_flight = False
        item.probe_token = None


@dataclass(frozen=True, slots=True)
class ProviderRouteSetting:
    provider: ProviderCode
    capability: ProviderCapability
    enabled: bool = True
    priority: int = 1
    concurrency: int = 2
    rate_per_second: float = 2.0
    timeout_seconds: float = 5.0
    auto_switch: bool = True


@dataclass(frozen=True, slots=True)
class ProviderRoutePlan:
    capability: ProviderCapability
    routes: tuple[ProviderRouteSetting, ...]
    fixed_provider: ProviderCode | None = None

    def __post_init__(self) -> None:
        if any(route.capability is not self.capability for route in self.routes):
            raise ValueError("route plan contains a different capability")


class ProviderConfigurationPort(Protocol):
    async def routes(
        self, capability: ProviderCapability
    ) -> tuple[ProviderRouteSetting, ...]: ...

    async def route_plan(self, capability: ProviderCapability) -> ProviderRoutePlan: ...


class StaticProviderConfiguration:
    def __init__(
        self,
        configured: dict[ProviderCapability, tuple[ProviderRouteSetting, ...]]
        | None = None,
    ) -> None:
        self._configured = configured or {
            ProviderCapability.REALTIME_QUOTE_BATCH: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.REALTIME_QUOTE_BATCH,
                    priority=1,
                ),
                ProviderRouteSetting(
                    ProviderCode.SINA,
                    ProviderCapability.REALTIME_QUOTE_BATCH,
                    priority=2,
                ),
                ProviderRouteSetting(
                    ProviderCode.TENCENT,
                    ProviderCapability.REALTIME_QUOTE_BATCH,
                    priority=3,
                ),
            ),
            ProviderCapability.SECURITY_MASTER: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.SECURITY_MASTER,
                    priority=0,
                    auto_switch=True,
                ),
                ProviderRouteSetting(
                    ProviderCode.SINA,
                    ProviderCapability.SECURITY_MASTER,
                    priority=1,
                    rate_per_second=1.0,
                    timeout_seconds=180.0,
                    auto_switch=False,
                ),
            ),
            ProviderCapability.DAILY_BAR_UNADJUSTED: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.DAILY_BAR_UNADJUSTED,
                ),
            ),
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED: (
                ProviderRouteSetting(
                    ProviderCode.SINA,
                    ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
                    concurrency=4,
                    rate_per_second=2.0,
                    timeout_seconds=300,
                    auto_switch=False,
                ),
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
                    priority=1,
                    auto_switch=False,
                ),
            ),
            ProviderCapability.HISTORICAL_DAILY_QFQ: (
                ProviderRouteSetting(
                    ProviderCode.SINA,
                    ProviderCapability.HISTORICAL_DAILY_QFQ,
                    concurrency=4,
                    rate_per_second=2.0,
                    timeout_seconds=300,
                    auto_switch=False,
                ),
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.HISTORICAL_DAILY_QFQ,
                    priority=1,
                    auto_switch=False,
                ),
            ),
            ProviderCapability.CORPORATE_ACTIONS: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.CORPORATE_ACTIONS,
                ),
            ),
        }

    async def routes(
        self, capability: ProviderCapability
    ) -> tuple[ProviderRouteSetting, ...]:
        return tuple(
            sorted(self._configured.get(capability, ()), key=lambda item: item.priority)
        )

    async def route_plan(self, capability: ProviderCapability) -> ProviderRoutePlan:
        return ProviderRoutePlan(capability, await self.routes(capability))


class ProviderRuntimeStatePort(Protocol):
    async def allow(
        self, setting: ProviderRouteSetting, *, probe: bool = False
    ) -> bool: ...
    async def acquire(self, setting: ProviderRouteSetting) -> ProviderLease | None: ...
    async def release(
        self, setting: ProviderRouteSetting, lease: ProviderLease
    ) -> None: ...
    async def record_success(self, setting: ProviderRouteSetting) -> None: ...
    async def record_failure(self, setting: ProviderRouteSetting) -> None: ...
    async def force_half_open(self, setting: ProviderRouteSetting) -> None: ...
    async def circuit_snapshot(
        self, setting: ProviderRouteSetting
    ) -> dict[str, Any]: ...


class ProviderRuntimeObserverPort(Protocol):
    async def record_half_open(
        self,
        setting: ProviderRouteSetting,
        *,
        snapshot: dict[str, Any],
        occurred_at: datetime,
        probe_token: str,
    ) -> None: ...

    async def record_outcome(
        self,
        setting: ProviderRouteSetting,
        *,
        success: bool,
        snapshot: dict[str, Any],
        occurred_at: datetime,
        error_code: str | None,
        latency_ms: int,
        switched: bool,
        response_sample: dict[str, Any] | None,
    ) -> None: ...


class NullProviderRuntimeObserver:
    async def record_half_open(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def record_outcome(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class ProviderCallError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProviderLease:
    backend: str
    token: str


def _capacity_name(setting: ProviderRouteSetting) -> str:
    if setting.capability in {
        ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
        ProviderCapability.HISTORICAL_DAILY_QFQ,
    }:
        return "HISTORICAL_DAILY"
    return setting.capability.value


def _runtime_capacity_key(
    setting: ProviderRouteSetting,
) -> tuple[ProviderCode, str]:
    return setting.provider, _capacity_name(setting)


class InMemoryProviderRuntimeState:
    """Conservative fallback and deterministic test implementation."""

    def __init__(
        self,
        *,
        global_limit: int = 4,
        realtime_reserved: int = 1,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._breaker = CircuitBreaker()
        self._global_limit = global_limit
        self._reserved = realtime_reserved
        self._active: dict[tuple[ProviderCode, str], int] = {}
        self._tokens: dict[tuple[ProviderCode, str], tuple[float, float]] = {}
        self._clock = clock
        self._lock = asyncio.Lock()

    async def allow(
        self, setting: ProviderRouteSetting, *, probe: bool = False
    ) -> bool:
        del probe
        return self._breaker.allow(
            setting.provider, setting.capability, now=datetime.now(UTC)
        )

    async def acquire(self, setting: ProviderRouteSetting) -> ProviderLease | None:
        async with self._lock:
            key = _runtime_capacity_key(setting)
            total = sum(self._active.values())
            usable = self._global_limit
            if setting.capability is not ProviderCapability.REALTIME_QUOTE_BATCH:
                usable = max(1, usable - self._reserved)
            if total >= usable or self._active.get(key, 0) >= setting.concurrency:
                return None
            now = self._clock()
            capacity = max(1.0, setting.rate_per_second)
            tokens, updated_at = self._tokens.get(key, (capacity, now))
            tokens = min(
                capacity,
                tokens + max(0.0, now - updated_at) * setting.rate_per_second,
            )
            if tokens < 1:
                self._tokens[key] = (tokens, now)
                return None
            self._tokens[key] = (tokens - 1, now)
            self._active[key] = self._active.get(key, 0) + 1
            return ProviderLease("local", uuid4().hex)

    async def release(
        self, setting: ProviderRouteSetting, lease: ProviderLease
    ) -> None:
        if lease.backend != "local":
            return
        async with self._lock:
            key = _runtime_capacity_key(setting)
            self._active[key] = max(0, self._active.get(key, 0) - 1)

    async def record_success(self, setting: ProviderRouteSetting) -> None:
        self._breaker.record_success(
            setting.provider, setting.capability, now=datetime.now(UTC)
        )

    async def record_failure(self, setting: ProviderRouteSetting) -> None:
        self._breaker.record_failure(
            setting.provider, setting.capability, now=datetime.now(UTC)
        )

    async def force_half_open(self, setting: ProviderRouteSetting) -> None:
        current = self._breaker.state(
            setting.provider, setting.capability, now=datetime.now(UTC)
        )
        if current is not CircuitState.HALF_OPEN:
            self._breaker.enable_for_probe(setting.provider, setting.capability)

    async def circuit_snapshot(self, setting: ProviderRouteSetting) -> dict[str, Any]:
        item = self._breaker._get(setting.provider, setting.capability)
        return {
            "state": self._breaker.state(
                setting.provider, setting.capability, now=datetime.now(UTC)
            ).value,
            "consecutive_failures": item.consecutive_failures,
            "cooldown_index": item.cooldown_index,
            "opened_at": item.opened_at,
            "probe_token": item.probe_token,
        }


class ProviderInvocationPipeline:
    def __init__(
        self,
        runtime: ProviderRuntimeStatePort,
        observer: ProviderRuntimeObserverPort | None = None,
    ) -> None:
        self._runtime = runtime
        self._observer = observer or NullProviderRuntimeObserver()

    async def call[T](
        self,
        setting: ProviderRouteSetting,
        operation: Callable[[], Awaitable[T]],
        *,
        deadline: datetime,
        probe: bool = False,
        observe: bool = True,
        switched: bool = False,
    ) -> T:
        started = monotonic()
        if not setting.enabled:
            raise ProviderCallError("PROVIDER_DISABLED")
        if not await self._runtime.allow(setting, probe=probe):
            if observe:
                await self._observer.record_outcome(
                    setting,
                    success=False,
                    snapshot=await self._runtime.circuit_snapshot(setting),
                    occurred_at=datetime.now(UTC),
                    error_code="PROVIDER_CIRCUIT_OPEN",
                    latency_ms=int((monotonic() - started) * 1000),
                    switched=False,
                    response_sample=None,
                )
            raise ProviderCallError("PROVIDER_CIRCUIT_OPEN")
        half_open_snapshot = await self._runtime.circuit_snapshot(setting)
        probe_token = half_open_snapshot.get("probe_token")
        if observe and half_open_snapshot.get("state") == "HALF_OPEN" and probe_token:
            await self._observer.record_half_open(
                setting,
                snapshot=half_open_snapshot,
                occurred_at=datetime.now(UTC),
                probe_token=str(probe_token),
            )
        lease = await self._runtime.acquire(setting)
        if lease is None:
            if observe:
                await self._observer.record_outcome(
                    setting,
                    success=False,
                    snapshot=await self._runtime.circuit_snapshot(setting),
                    occurred_at=datetime.now(UTC),
                    error_code="PROVIDER_RATE_LIMITED",
                    latency_ms=int((monotonic() - started) * 1000),
                    switched=False,
                    response_sample=None,
                )
            raise ProviderCallError("PROVIDER_RATE_LIMITED")
        try:
            try:
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                timeout = min(setting.timeout_seconds, remaining)
                if timeout <= 0:
                    raise TimeoutError("provider deadline expired")
                async with asyncio.timeout(timeout):
                    context_token = enter_request_context(setting)
                    try:
                        result = await operation()
                    finally:
                        exit_request_context(context_token)
            except Exception as error:
                await self._runtime.record_failure(setting)
                if observe:
                    await self._observer.record_outcome(
                        setting,
                        success=False,
                        snapshot=await self._runtime.circuit_snapshot(setting),
                        occurred_at=datetime.now(UTC),
                        error_code=getattr(error, "code", "PROVIDER_FAILED"),
                        latency_ms=int((monotonic() - started) * 1000),
                        switched=switched,
                        response_sample=getattr(error, "response_sample", None),
                    )
                raise
            batch_error = getattr(result, "batch_error_code", None)
            unhealthy_probe = getattr(result, "healthy", True) is False
            if batch_error or unhealthy_probe:
                await self._runtime.record_failure(setting)
            else:
                await self._runtime.record_success(setting)
            if observe:
                await self._observer.record_outcome(
                    setting,
                    success=not (batch_error or unhealthy_probe),
                    snapshot=await self._runtime.circuit_snapshot(setting),
                    occurred_at=datetime.now(UTC),
                    error_code=batch_error or getattr(result, "error_code", None),
                    latency_ms=int((monotonic() - started) * 1000),
                    switched=switched,
                    response_sample=None,
                )
            return result
        finally:
            await self._runtime.release(setting, lease)
