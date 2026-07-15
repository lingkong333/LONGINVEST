from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

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
            return
        item.consecutive_failures += 1
        if item.consecutive_failures >= 3:
            item.state = CircuitState.OPEN
            item.opened_at = now
            item.probe_in_flight = False

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

    def disable(self, provider: ProviderCode, capability: ProviderCapability) -> None:
        item = self._get(provider, capability)
        item.state = CircuitState.DISABLED
        item.probe_in_flight = False

    def enable_for_probe(
        self, provider: ProviderCode, capability: ProviderCapability
    ) -> None:
        item = self._get(provider, capability)
        item.state = CircuitState.HALF_OPEN
        item.probe_in_flight = False


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
