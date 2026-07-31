from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text

from long_invest.modules.providers.models import (
    ProviderCircuitHistory,
    ProviderCircuitState,
)
from long_invest.modules.providers.resilience import (
    CircuitState,
    ProviderLease,
    ProviderRouteSetting,
)


class PostgresProviderRuntimeState:
    """Persistent circuit gate; request budgets own rate and concurrency limits."""

    _cooldowns = (60, 180, 300)
    _probe_timeout = timedelta(seconds=30)

    def __init__(self, database: Any) -> None:
        self._database = database
        self._transition_from: ContextVar[str | None] = ContextVar(
            "provider_circuit_transition_from", default=None
        )

    async def allow(
        self, setting: ProviderRouteSetting, *, probe: bool = False
    ) -> bool:
        self._transition_from.set(None)
        async with self._database.transaction() as session:
            await self._lock(session, setting)
            now = await self._now(session)
            circuit = await self._circuit(session, setting, create=True)
            assert circuit is not None
            if circuit.state == CircuitState.DISABLED.value:
                return False
            if circuit.state == CircuitState.CLOSED.value:
                return True
            if circuit.state == CircuitState.OPEN.value:
                cooldown = self._cooldowns[circuit.cooldown_index]
                ready = (
                    probe
                    or circuit.opened_at is None
                    or now >= circuit.opened_at + timedelta(seconds=cooldown)
                )
                if not ready:
                    return False
            elif (
                circuit.state == CircuitState.HALF_OPEN.value
                and now < circuit.updated_at + self._probe_timeout
            ):
                return False
            previous_state = circuit.state
            circuit.state = CircuitState.HALF_OPEN.value
            circuit.updated_at = now
            self._record_transition(
                session,
                setting,
                previous_state=previous_state,
                circuit=circuit,
                reason_code="PROVIDER_HALF_OPEN_PROBE_GRANTED",
                occurred_at=now,
            )
            return True

    async def acquire(self, setting: ProviderRouteSetting) -> ProviderLease:
        del setting
        return ProviderLease("postgres", uuid4().hex)

    async def release(
        self, setting: ProviderRouteSetting, lease: ProviderLease
    ) -> None:
        del setting, lease

    async def record_success(self, setting: ProviderRouteSetting) -> None:
        async with self._database.transaction() as session:
            await self._lock(session, setting)
            now = await self._now(session)
            circuit = await self._circuit(session, setting, create=True)
            assert circuit is not None
            if circuit.state == CircuitState.DISABLED.value:
                return
            previous_state = circuit.state
            circuit.state = CircuitState.CLOSED.value
            circuit.consecutive_failures = 0
            circuit.cooldown_index = 0
            circuit.opened_at = None
            self._record_transition(
                session,
                setting,
                previous_state=previous_state,
                circuit=circuit,
                reason_code="PROVIDER_REQUEST_SUCCEEDED",
                occurred_at=now,
            )

    async def record_failure(self, setting: ProviderRouteSetting) -> None:
        async with self._database.transaction() as session:
            await self._lock(session, setting)
            now = await self._now(session)
            circuit = await self._circuit(session, setting, create=True)
            assert circuit is not None
            if circuit.state == CircuitState.DISABLED.value:
                return
            if circuit.state == CircuitState.HALF_OPEN.value:
                previous_state = circuit.state
                circuit.cooldown_index = min(circuit.cooldown_index + 1, 2)
                circuit.consecutive_failures = 3
                circuit.state = CircuitState.OPEN.value
                circuit.opened_at = now
                self._record_transition(
                    session,
                    setting,
                    previous_state=previous_state,
                    circuit=circuit,
                    reason_code="PROVIDER_REQUEST_FAILED",
                    occurred_at=now,
                )
                return
            circuit.consecutive_failures += 1
            if circuit.consecutive_failures >= 3:
                previous_state = circuit.state
                circuit.state = CircuitState.OPEN.value
                circuit.opened_at = now
                self._record_transition(
                    session,
                    setting,
                    previous_state=previous_state,
                    circuit=circuit,
                    reason_code="PROVIDER_REQUEST_FAILED",
                    occurred_at=now,
                )

    async def force_half_open(self, setting: ProviderRouteSetting) -> None:
        async with self._database.transaction() as session:
            await self._lock(session, setting)
            circuit = await self._circuit(session, setting, create=True)
            assert circuit is not None
            if circuit.state != CircuitState.DISABLED.value:
                circuit.state = CircuitState.OPEN.value
                circuit.opened_at = datetime(1970, 1, 1, tzinfo=UTC)

    async def circuit_snapshot(
        self, setting: ProviderRouteSetting
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            circuit = await self._circuit(session, setting, create=False)
            if circuit is None:
                return {
                    "state": CircuitState.CLOSED.value,
                    "consecutive_failures": 0,
                    "cooldown_index": 0,
                    "opened_at": None,
                    "probe_token": None,
                }
            return {
                "state": circuit.state,
                "consecutive_failures": circuit.consecutive_failures,
                "cooldown_index": circuit.cooldown_index,
                "opened_at": circuit.opened_at,
                "probe_token": (
                    self._stable_probe_token(setting, circuit.updated_at)
                    if circuit.state == CircuitState.HALF_OPEN.value
                    else None
                ),
                "transition_from": self._transition_from.get(),
                "transition_persisted": self._transition_from.get() is not None,
            }

    def _record_transition(
        self,
        session: Any,
        setting: ProviderRouteSetting,
        *,
        previous_state: str,
        circuit: ProviderCircuitState,
        reason_code: str,
        occurred_at: datetime,
    ) -> None:
        if previous_state == circuit.state:
            return
        session.add(
            ProviderCircuitHistory(
                provider_code=setting.provider.value,
                capability=setting.capability.value,
                from_state=previous_state,
                to_state=circuit.state,
                reason_code=reason_code,
                occurred_at=occurred_at,
            )
        )
        self._transition_from.set(previous_state)

    @staticmethod
    def _stable_probe_token(
        setting: ProviderRouteSetting, granted_at: datetime
    ) -> str:
        return (
            f"{setting.provider.value}:{setting.capability.value}:"
            f"{granted_at.isoformat()}"
        )

    @staticmethod
    async def _lock(session: Any, setting: ProviderRouteSetting) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {
                "key": (
                    f"provider-circuit:{setting.provider.value}:"
                    f"{setting.capability.value}"
                )
            },
        )

    @staticmethod
    async def _now(session: Any) -> datetime:
        now = await session.scalar(select(func.clock_timestamp()))
        return now if isinstance(now, datetime) else datetime.now(UTC)

    @staticmethod
    async def _circuit(
        session: Any,
        setting: ProviderRouteSetting,
        *,
        create: bool,
    ) -> ProviderCircuitState | None:
        circuit = await session.scalar(
            select(ProviderCircuitState)
            .where(
                ProviderCircuitState.provider_code == setting.provider.value,
                ProviderCircuitState.capability == setting.capability.value,
            )
            .with_for_update()
        )
        if circuit is None and create:
            circuit = ProviderCircuitState(
                provider_code=setting.provider.value,
                capability=setting.capability.value,
                state=CircuitState.CLOSED.value,
                consecutive_failures=0,
                cooldown_index=0,
            )
            session.add(circuit)
            await session.flush()
        return circuit
