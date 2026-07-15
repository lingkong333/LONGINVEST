from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.models import ProviderCircuitHistory, ProviderFailureSample

SENSITIVE_KEYS = frozenset({"token", "cookie", "authorization", "header", "headers", "password", "secret"})


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return value[:256]
    return value


def redact_failure_sample(
    *, provider: ProviderCode, capability: ProviderCapability, error_code: str,
    payload: dict[str, Any], now: datetime,
) -> ProviderFailureSample:
    return ProviderFailureSample(
        provider_code=provider.value,
        capability=capability.value,
        error_code=error_code,
        sample=_redact(payload),
        created_at=now,
        expires_at=now + timedelta(days=7),
    )


class ProviderEventPort(Protocol):
    async def append(self, event_type: str, payload: dict[str, Any]) -> None: ...


class ProviderRepository:
    def __init__(self, session: AsyncSession, events: ProviderEventPort | None = None) -> None:
        self._session = session
        self._events = events

    async def add_circuit_transition(self, history: ProviderCircuitHistory) -> None:
        self._session.add(history)
        if self._events is not None:
            await self._events.append(
                "provider.circuit_state_changed",
                {"provider": history.provider_code, "capability": history.capability, "state": history.to_state},
            )

    async def add_failure_sample(self, sample: ProviderFailureSample) -> None:
        self._session.add(sample)

    async def flush(self) -> None:
        await self._session.flush()
