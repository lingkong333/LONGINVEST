from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

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


class ProviderRateLimiter:
    def __init__(
        self, *, global_limit: int, capability_limit: int, realtime_reserved: int
    ) -> None:
        if (
            min(global_limit, capability_limit) <= 0
            or not 0 <= realtime_reserved < global_limit
        ):
            raise ValueError("invalid rate limits")
        self._configured_global = global_limit
        self._configured_capability = capability_limit
        self._reserved = realtime_reserved
        self._global_limit = global_limit
        self._capability_limit = capability_limit
        self._active: dict[ProviderCapability, int] = {}

    def acquire(self, capability: ProviderCapability) -> bool:
        total = sum(self._active.values())
        active = self._active.get(capability, 0)
        is_realtime = capability is ProviderCapability.REALTIME_QUOTE_BATCH
        usable_global = (
            self._global_limit
            if is_realtime
            else max(1, self._global_limit - self._reserved)
        )
        if total >= usable_global or active >= self._capability_limit:
            return False
        self._active[capability] = active + 1
        return True

    def release(self, capability: ProviderCapability) -> None:
        self._active[capability] = max(0, self._active.get(capability, 0) - 1)

    def redis_failed(self) -> None:
        self._global_limit = 1
        self._capability_limit = 1
        self._reserved = 0


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


class ProviderConfigurationPort(Protocol):
    async def routes(
        self, capability: ProviderCapability
    ) -> tuple[ProviderRouteSetting, ...]: ...


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
            ),
            ProviderCapability.SECURITY_MASTER: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.SECURITY_MASTER,
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
                    ProviderCode.EASTMONEY,
                    ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
                ),
            ),
            ProviderCapability.HISTORICAL_DAILY_QFQ: (
                ProviderRouteSetting(
                    ProviderCode.EASTMONEY,
                    ProviderCapability.HISTORICAL_DAILY_QFQ,
                ),
            ),
        }

    async def routes(
        self, capability: ProviderCapability
    ) -> tuple[ProviderRouteSetting, ...]:
        return tuple(
            sorted(self._configured.get(capability, ()), key=lambda item: item.priority)
        )


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
        self._active: dict[tuple[ProviderCode, ProviderCapability], int] = {}
        self._tokens: dict[
            tuple[ProviderCode, ProviderCapability], tuple[float, float]
        ] = {}
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
            key = (setting.provider, setting.capability)
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
            key = (setting.provider, setting.capability)
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


class RedisProviderRuntimeState:
    """Shared Redis state with a conservative per-process fallback."""

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = "provider",
        global_limit: int = 4,
        realtime_reserved: int = 1,
    ) -> None:
        self._redis = redis
        self._namespace = namespace
        self._global_limit = global_limit
        self._reserved = realtime_reserved
        self._fallback = InMemoryProviderRuntimeState(
            global_limit=1, realtime_reserved=0
        )

    def _circuit_key(self, setting: ProviderRouteSetting) -> str:
        return f"{self._namespace}:circuit:{setting.provider}:{setting.capability}"

    async def allow(
        self, setting: ProviderRouteSetting, *, probe: bool = False
    ) -> bool:
        script = """
        local raw=redis.call('get',KEYS[1])
        if not raw then return 1 end
        local state=cjson.decode(raw)
        if state['state']=='CLOSED' then return 1 end
        if state['state']=='DISABLED' then return 0 end
        local level=math.min(tonumber(state['level'] or 0),2)
        local cooldown=tonumber(ARGV[3+level])
        local opened=tonumber(state['opened_at'] or 0)
        local ready=(ARGV[1]=='1') or ((tonumber(ARGV[2])-opened)>=cooldown)
        if not ready or redis.call('exists',KEYS[2])==1 then return 0 end
        redis.call('set',KEYS[2],ARGV[6],'EX',30)
        state['state']='HALF_OPEN'
        state['probe_token']=ARGV[6]
        redis.call('set',KEYS[1],cjson.encode(state))
        return 1
        """
        try:
            circuit_key = self._circuit_key(setting)
            return bool(
                await self._redis.eval(
                    script,
                    2,
                    circuit_key,
                    f"{circuit_key}:probe",
                    "1" if probe else "0",
                    datetime.now(UTC).timestamp(),
                    60,
                    180,
                    300,
                    uuid4().hex,
                )
            )
        except Exception:
            return await self._fallback.allow(setting, probe=probe)

    async def acquire(self, setting: ProviderRouteSetting) -> ProviderLease | None:
        script = """
        local total=tonumber(redis.call('get',KEYS[1]) or '0')
        local cap=tonumber(redis.call('get',KEYS[2]) or '0')
        if total>=tonumber(ARGV[1]) or cap>=tonumber(ARGV[2]) then return 0 end
        local clock=redis.call('TIME')
        local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
        local rate=tonumber(ARGV[3])
        local capacity=math.max(1000,rate)
        local tokens=tonumber(redis.call('hget',KEYS[3],'tokens') or capacity)
        local updated=tonumber(redis.call('hget',KEYS[3],'updated') or now)
        tokens=math.min(capacity,tokens+math.floor((now-updated)*rate/1000))
        if tokens<1000 then
          redis.call('hmset',KEYS[3],'tokens',tokens,'updated',now)
          redis.call('expire',KEYS[3],math.max(2,math.ceil(2000/rate)))
          return 0
        end
        tokens=tokens-1000
        redis.call('hmset',KEYS[3],'tokens',tokens,'updated',now)
        redis.call('expire',KEYS[3],math.max(2,math.ceil(2000/rate)))
        redis.call('incr',KEYS[1]); redis.call('expire',KEYS[1],30)
        redis.call('incr',KEYS[2]); redis.call('expire',KEYS[2],30)
        return 1
        """
        limit = self._global_limit
        if setting.capability is not ProviderCapability.REALTIME_QUOTE_BATCH:
            limit = max(1, limit - self._reserved)
        keys = [
            f"{self._namespace}:active:global",
            f"{self._namespace}:active:{setting.provider}:{setting.capability}",
            f"{self._namespace}:rate:{setting.provider}:{setting.capability}",
        ]
        try:
            acquired = bool(
                await self._redis.eval(
                    script,
                    len(keys),
                    *keys,
                    limit,
                    setting.concurrency,
                    max(1, round(setting.rate_per_second * 1000)),
                )
            )
            return ProviderLease("redis", uuid4().hex) if acquired else None
        except Exception:
            return await self._fallback.acquire(setting)

    async def release(
        self, setting: ProviderRouteSetting, lease: ProviderLease
    ) -> None:
        if lease.backend == "local":
            await self._fallback.release(setting, lease)
            return
        keys = [
            f"{self._namespace}:active:global",
            f"{self._namespace}:active:{setting.provider}:{setting.capability}",
        ]
        script = """
        for i=1,#KEYS do
          local value=tonumber(redis.call('get',KEYS[i]) or '0')
          if value>0 then redis.call('decr',KEYS[i]) end
        end
        return 1
        """
        try:
            await self._redis.eval(script, len(keys), *keys)
        except Exception:
            return

    async def record_success(self, setting: ProviderRouteSetting) -> None:
        script = """
        redis.call('set',KEYS[1],ARGV[1])
        redis.call('del',KEYS[2])
        return 1
        """
        try:
            circuit_key = self._circuit_key(setting)
            await self._redis.eval(
                script,
                2,
                circuit_key,
                f"{circuit_key}:probe",
                json.dumps({"state": "CLOSED", "failures": 0, "level": 0}),
            )
        except Exception:
            await self._fallback.record_success(setting)

    async def record_failure(self, setting: ProviderRouteSetting) -> None:
        script = """
        local raw=redis.call('get',KEYS[1])
        local state=raw and cjson.decode(raw) or {state='CLOSED',failures=0,level=0}
        local failures=tonumber(state['failures'] or 0)+1
        if state['state']=='HALF_OPEN' then
          state['level']=math.min(tonumber(state['level'] or 0)+1,2)
          failures=3
        end
        state['failures']=failures
        if failures>=3 then
          state['state']='OPEN'
          state['opened_at']=tonumber(ARGV[1])
        end
        redis.call('set',KEYS[1],cjson.encode(state))
        redis.call('del',KEYS[2])
        return 1
        """
        try:
            circuit_key = self._circuit_key(setting)
            await self._redis.eval(
                script,
                2,
                circuit_key,
                f"{circuit_key}:probe",
                datetime.now(UTC).timestamp(),
            )
        except Exception:
            await self._fallback.record_failure(setting)

    async def force_half_open(self, setting: ProviderRouteSetting) -> None:
        script = """
        local raw=redis.call('get',KEYS[1])
        local state=raw and cjson.decode(raw) or {failures=3,level=0}
        state['state']='OPEN'
        state['opened_at']=0
        redis.call('set',KEYS[1],cjson.encode(state))
        return 1
        """
        try:
            await self._redis.eval(
                script, 1, self._circuit_key(setting)
            )
        except Exception:
            await self._fallback.force_half_open(setting)

    async def circuit_snapshot(self, setting: ProviderRouteSetting) -> dict[str, Any]:
        try:
            raw = await self._redis.get(self._circuit_key(setting))
            return (
                json.loads(raw)
                if raw
                else {"state": "CLOSED", "failures": 0, "level": 0}
            )
        except Exception:
            return await self._fallback.circuit_snapshot(setting)


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
        if (
            observe
            and half_open_snapshot.get("state") == "HALF_OPEN"
            and probe_token
        ):
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
                    result = await operation()
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
