from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.models import (
    ProviderCapabilitySetting,
    ProviderCircuitHistory,
    ProviderCircuitState,
    ProviderConfigVersion,
    ProviderFailureSample,
    ProviderHealthState,
    ProviderMutationRequest,
)
from long_invest.modules.providers.resilience import ProviderRouteSetting
from long_invest.platform.errors import AppError

SENSITIVE_KEYS = frozenset(
    {"token", "cookie", "authorization", "header", "headers", "password", "secret"}
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return value[:256]
    return value


def request_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def redact_failure_sample(
    *,
    provider: ProviderCode,
    capability: ProviderCapability,
    error_code: str,
    payload: dict[str, Any],
    now: datetime,
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
    async def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> None: ...


class ProviderAuditPort(Protocol):
    async def record(
        self,
        *,
        context: AuditContext,
        action_code: str,
        object_type: str,
        object_id: str,
        reason: str,
        before_summary: dict[str, Any] | None,
        after_summary: dict[str, Any] | None,
    ) -> None: ...


class ProviderRepositoryPort(Protocol):
    async def routes(
        self, capability: ProviderCapability
    ) -> tuple[ProviderRouteSetting, ...]: ...
    async def list_provider_codes(self) -> tuple[ProviderCode, ...]: ...
    async def provider_summary(self, provider: ProviderCode) -> dict[str, Any]: ...
    async def health(self, provider: ProviderCode) -> list[dict[str, Any]]: ...
    async def circuits(self) -> list[dict[str, Any]]: ...
    async def circuit(self, circuit_id: UUID) -> dict[str, Any]: ...
    async def mutate_settings(
        self,
        provider: ProviderCode,
        capabilities: frozenset[ProviderCapability],
        changes: dict[str, Any],
        *,
        expected_version: int,
        reason: str,
        context: AuditContext,
    ) -> dict[str, Any]: ...
    async def persist_probe(
        self,
        circuit_id: UUID,
        result: dict[str, Any],
        *,
        action_code: str,
        reason: str,
        context: AuditContext,
    ) -> dict[str, Any]: ...
    async def audit_diagnostic(
        self,
        symbols: tuple[str, ...],
        summary: dict[str, Any],
        *,
        reason: str,
        context: AuditContext,
    ) -> None: ...
    async def record_outcome(
        self,
        setting: ProviderRouteSetting,
        *,
        success: bool,
        snapshot: dict[str, Any],
        occurred_at: datetime,
        error_code: str | None,
        latency_ms: int = 0,
        switched: bool = False,
        response_sample: dict[str, Any] | None = None,
    ) -> None: ...
    async def replay_mutation(
        self, idempotency_key: str, digest: str
    ) -> dict[str, Any] | None: ...


class ProviderRepository:
    """SQLAlchemy repository; audit and outbox share this session transaction."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        audit: ProviderAuditPort,
        events: ProviderEventPort,
    ) -> None:
        if audit is None or events is None:
            raise ValueError("provider writes require audit and outbox ports")
        self._session = session
        self._audit = audit
        self._events = events

    async def routes(
        self, capability: ProviderCapability
    ) -> tuple[ProviderRouteSetting, ...]:
        latest = (
            select(
                ProviderCapabilitySetting.provider_code.label("provider_code"),
                func.max(ProviderCapabilitySetting.config_version).label("version"),
            )
            .where(ProviderCapabilitySetting.capability == capability.value)
            .group_by(ProviderCapabilitySetting.provider_code)
            .subquery()
        )
        async with self._session.begin():
            rows = await self._session.scalars(
                select(ProviderCapabilitySetting)
                .join(
                    latest,
                    (
                        ProviderCapabilitySetting.provider_code
                        == latest.c.provider_code
                    )
                    & (
                        ProviderCapabilitySetting.config_version
                        == latest.c.version
                    ),
                )
                .where(ProviderCapabilitySetting.capability == capability.value)
                .order_by(ProviderCapabilitySetting.priority)
            )
            return tuple(self._route(row) for row in rows)

    async def list_provider_codes(self) -> tuple[ProviderCode, ...]:
        async with self._session.begin():
            rows = await self._session.scalars(
                select(ProviderConfigVersion.provider_code).distinct()
            )
            return tuple(ProviderCode(item) for item in rows)

    async def provider_summary(self, provider: ProviderCode) -> dict[str, Any]:
        async with self._session.begin():
            current = await self._latest_config(provider)
            if current is None:
                raise AppError(
                    code="PROVIDER_NOT_FOUND",
                    message="Provider 不存在",
                    status_code=404,
                )
            settings = await self._settings(provider, current.version)
            return {
                "provider_code": provider.value,
                "version": current.version,
                "reason": current.reason,
                "capabilities": [self._setting_dict(row) for row in settings],
            }

    async def health(self, provider: ProviderCode) -> list[dict[str, Any]]:
        async with self._session.begin():
            rows = await self._session.scalars(
                select(ProviderHealthState).where(
                    ProviderHealthState.provider_code == provider.value
                )
            )
            return [
                {
                    "capability": row.capability,
                    "status": row.status,
                    "consecutive_failures": row.consecutive_failures,
                    "last_success_at": row.last_success_at,
                    "last_failure_at": row.last_failure_at,
                    "metrics": row.metrics,
                }
                for row in rows
            ]

    async def circuits(self) -> list[dict[str, Any]]:
        async with self._session.begin():
            rows = await self._session.scalars(select(ProviderCircuitState))
            return [self._circuit_dict(row) for row in rows]

    async def circuit(self, circuit_id: UUID) -> dict[str, Any]:
        async with self._session.begin():
            row = await self._session.scalar(
                select(ProviderCircuitState).where(
                    ProviderCircuitState.id == circuit_id
                )
            )
            if row is None:
                raise AppError(
                    code="PROVIDER_CIRCUIT_NOT_FOUND",
                    message="熔断记录不存在",
                    status_code=404,
                )
            return self._circuit_dict(row)

    async def mutate_settings(
        self,
        provider: ProviderCode,
        capabilities: frozenset[ProviderCapability],
        changes: dict[str, Any],
        *,
        expected_version: int,
        reason: str,
        context: AuditContext,
    ) -> dict[str, Any]:
        payload = {
            "operation": "settings",
            "provider": provider.value,
            "expected_version": expected_version,
            "changes": changes,
            "reason": reason,
        }
        digest = request_digest(payload)
        async with self._session.begin():
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:provider_key))"),
                {"provider_key": f"provider-config:{provider.value}"},
            )
            replay = await self._idempotent_replay(context.idempotency_key, digest)
            if replay is not None:
                return replay
            current = await self._session.scalar(
                select(ProviderConfigVersion)
                .where(ProviderConfigVersion.provider_code == provider.value)
                .order_by(ProviderConfigVersion.version.desc())
                .limit(1)
                .with_for_update()
            )
            current_version = current.version if current else 0
            if current_version != expected_version:
                raise AppError(
                    code="PROVIDER_CONFIG_VERSION_CONFLICT",
                    message="Provider 配置已更新，请刷新后重试",
                    status_code=409,
                    details={"current_version": current_version},
                )
            previous_rows = (
                await self._settings(provider, current_version)
                if current_version
                else []
            )
            previous = {
                ProviderCapability(row.capability): self._setting_dict(row)
                for row in previous_rows
            }
            next_version = current_version + 1
            next_config = ProviderConfigVersion(
                provider_code=provider.value,
                version=next_version,
                reason=reason,
            )
            self._session.add(next_config)
            after_capabilities: list[dict[str, Any]] = []
            for capability in sorted(capabilities, key=lambda item: item.value):
                values = {
                    "enabled": True,
                    "priority": 1,
                    "concurrency": 2,
                    "rate_per_second": 2.0,
                    "timeout_seconds": 5.0,
                    "auto_switch": True,
                    **previous.get(capability, {}),
                    **changes,
                }
                values.pop("capability", None)
                row = ProviderCapabilitySetting(
                    config_version=next_version,
                    provider_code=provider.value,
                    capability=capability.value,
                    **values,
                )
                self._session.add(row)
                after_capabilities.append(self._setting_dict(row))
            before = {
                "version": current_version,
                "capabilities": list(previous.values()),
            }
            after = {"version": next_version, "capabilities": after_capabilities}
            self._session.add(
                self._mutation(
                    context,
                    digest=digest,
                    operation="settings",
                    object_id=provider.value,
                    response=after,
                )
            )
            await self._audit.record(
                context=context,
                action_code="provider.config_changed",
                object_type="provider",
                object_id=provider.value,
                reason=reason,
                before_summary=before,
                after_summary=after,
            )
            await self._events.append(
                "provider.config_changed",
                {"provider": provider.value, "version": next_version},
                idempotency_key=context.idempotency_key,
            )
            await self._session.flush()
            return after

    async def persist_probe(
        self,
        circuit_id: UUID,
        result: dict[str, Any],
        *,
        action_code: str,
        reason: str,
        context: AuditContext,
    ) -> dict[str, Any]:
        payload = {
            "operation": action_code,
            "circuit_id": str(circuit_id),
            "reason": reason,
        }
        digest = request_digest(payload)
        async with self._session.begin():
            replay = await self._idempotent_replay(context.idempotency_key, digest)
            if replay is not None:
                return replay
            circuit = await self._session.scalar(
                select(ProviderCircuitState)
                .where(ProviderCircuitState.id == circuit_id)
                .with_for_update()
            )
            if circuit is None:
                raise AppError(
                    code="PROVIDER_CIRCUIT_NOT_FOUND",
                    message="熔断记录不存在",
                    status_code=404,
                )
            before = self._circuit_dict(circuit)
            circuit.state = result["state"]
            circuit.consecutive_failures = result["consecutive_failures"]
            circuit.cooldown_index = result["cooldown_index"]
            circuit.opened_at = result.get("opened_at")
            health = await self._session.scalar(
                select(ProviderHealthState)
                .where(
                    ProviderHealthState.provider_code == circuit.provider_code,
                    ProviderHealthState.capability == circuit.capability,
                )
                .with_for_update()
            )
            if health is None:
                health = ProviderHealthState(
                    provider_code=circuit.provider_code,
                    capability=circuit.capability,
                    status="UNKNOWN",
                    consecutive_failures=0,
                    metrics={},
                )
                self._session.add(health)
            health.status = "HEALTHY" if result["healthy"] else "CIRCUIT_OPEN"
            health.consecutive_failures = result["consecutive_failures"]
            if result["healthy"]:
                health.last_success_at = result["checked_at"]
            else:
                health.last_failure_at = result["checked_at"]
            self._session.add(
                ProviderCircuitHistory(
                    provider_code=circuit.provider_code,
                    capability=circuit.capability,
                    from_state=before["state"],
                    to_state=circuit.state,
                    reason_code=action_code,
                    occurred_at=result["checked_at"],
                )
            )
            after = self._circuit_dict(circuit)
            self._session.add(
                self._mutation(
                    context,
                    digest=digest,
                    operation=action_code,
                    object_id=str(circuit_id),
                    response=after,
                )
            )
            await self._audit.record(
                context=context,
                action_code=action_code,
                object_type="provider_circuit",
                object_id=str(circuit_id),
                reason=reason,
                before_summary=before,
                after_summary=after,
            )
            await self._events.append(
                action_code,
                {"circuit_id": str(circuit_id), "state": circuit.state},
                idempotency_key=context.idempotency_key,
            )
            await self._session.flush()
            return after

    async def audit_diagnostic(
        self,
        symbols: tuple[str, ...],
        summary: dict[str, Any],
        *,
        reason: str,
        context: AuditContext,
    ) -> None:
        payload = {
            "operation": "diagnostic",
            "symbols": symbols,
            "reason": reason,
        }
        digest = request_digest(payload)
        async with self._session.begin():
            replay = await self._idempotent_replay(context.idempotency_key, digest)
            if replay is not None:
                return
            self._session.add(
                self._mutation(
                    context,
                    digest=digest,
                    operation="diagnostic",
                    object_id=",".join(symbols),
                    response=summary,
                )
            )
            await self._audit.record(
                context=context,
                action_code="provider.quote_diagnostics",
                object_type="security_batch",
                object_id=",".join(symbols),
                reason=reason,
                before_summary=None,
                after_summary=summary,
            )
            await self._events.append(
                "provider.quote_diagnostics",
                {"symbols": symbols},
                idempotency_key=context.idempotency_key,
            )
            await self._session.flush()

    async def record_outcome(
        self,
        setting: ProviderRouteSetting,
        *,
        success: bool,
        snapshot: dict[str, Any],
        occurred_at: datetime,
        error_code: str | None,
        latency_ms: int = 0,
        switched: bool = False,
        response_sample: dict[str, Any] | None = None,
    ) -> None:
        event_key = (
            f"runtime:{setting.provider.value}:{setting.capability.value}:"
            f"{occurred_at.isoformat()}"
        )
        async with self._session.begin():
            health = await self._session.scalar(
                select(ProviderHealthState)
                .where(
                    ProviderHealthState.provider_code == setting.provider.value,
                    ProviderHealthState.capability == setting.capability.value,
                )
                .with_for_update()
            )
            if health is None:
                health = ProviderHealthState(
                    provider_code=setting.provider.value,
                    capability=setting.capability.value,
                    status="UNKNOWN",
                    consecutive_failures=0,
                    metrics={},
                )
                self._session.add(health)
            failures = snapshot.get("consecutive_failures", snapshot.get("failures", 0))
            state = snapshot.get("state", "UNKNOWN")
            health.status = (
                "HEALTHY"
                if success
                else ("CIRCUIT_OPEN" if state == "OPEN" else "DEGRADED")
            )
            health.consecutive_failures = int(failures)
            if success:
                health.last_success_at = occurred_at
            else:
                health.last_failure_at = occurred_at
            metrics = dict(health.metrics)
            request_count = int(metrics.get("request_count", 0)) + 1
            success_count = int(metrics.get("success_count", 0)) + int(success)
            samples = [*metrics.get("latency_samples_ms", []), latency_ms][-100:]
            ordered_samples = sorted(int(item) for item in samples)
            p95_index = max(0, (len(ordered_samples) * 95 + 99) // 100 - 1)
            opened_at = snapshot.get("opened_at")
            cooldown_index = int(
                snapshot.get("cooldown_index", snapshot.get("level", 0))
            )
            cooldown_remaining = self._cooldown_remaining_seconds(
                opened_at, cooldown_index, occurred_at
            )
            health.metrics = {
                **metrics,
                "last_error_code": error_code,
                "request_count": request_count,
                "success_count": success_count,
                "success_rate": success_count / request_count,
                "latency_samples_ms": samples,
                "p95_latency_ms": ordered_samples[p95_index],
                "rate_limit_wait_ms": int(metrics.get("rate_limit_wait_ms", 0)),
                "switch_count": int(metrics.get("switch_count", 0)) + int(switched),
                "schema_error_count": int(metrics.get("schema_error_count", 0))
                + int(error_code == "PROVIDER_SCHEMA_INCOMPATIBLE"),
                "cooldown_remaining_seconds": cooldown_remaining,
            }

            if error_code == "PROVIDER_SCHEMA_INCOMPATIBLE":
                self._session.add(
                    redact_failure_sample(
                        provider=setting.provider,
                        capability=setting.capability,
                        error_code=error_code,
                        payload=response_sample
                        or {
                            "provider": setting.provider.value,
                            "capability": setting.capability.value,
                            "error_code": error_code,
                        },
                        now=occurred_at,
                    )
                )
                await self._events.append(
                    "provider.schema_changed",
                    {
                        "alert_code": "PROVIDER_SCHEMA_CHANGED",
                        "provider": setting.provider.value,
                        "capability": setting.capability.value,
                    },
                    idempotency_key=f"{event_key}:schema",
                )

            circuit = await self._session.scalar(
                select(ProviderCircuitState)
                .where(
                    ProviderCircuitState.provider_code == setting.provider.value,
                    ProviderCircuitState.capability == setting.capability.value,
                )
                .with_for_update()
            )
            previous_state = "UNKNOWN"
            if circuit is None:
                circuit = ProviderCircuitState(
                    provider_code=setting.provider.value,
                    capability=setting.capability.value,
                    state=state,
                    consecutive_failures=int(failures),
                    cooldown_index=int(
                        snapshot.get("cooldown_index", snapshot.get("level", 0))
                    ),
                    opened_at=snapshot.get("opened_at"),
                )
                self._session.add(circuit)
            else:
                previous_state = circuit.state
                circuit.state = state
                circuit.consecutive_failures = int(failures)
                circuit.cooldown_index = int(
                    snapshot.get("cooldown_index", snapshot.get("level", 0))
                )
                circuit.opened_at = snapshot.get("opened_at")
            if previous_state != state:
                self._session.add(
                    ProviderCircuitHistory(
                        provider_code=setting.provider.value,
                        capability=setting.capability.value,
                        from_state=previous_state,
                        to_state=state,
                        reason_code=error_code or "PROVIDER_REQUEST_SUCCEEDED",
                        occurred_at=occurred_at,
                    )
                )
                await self._events.append(
                    "provider.circuit_state_changed",
                    {
                        "provider": setting.provider.value,
                        "capability": setting.capability.value,
                        "from_state": previous_state,
                        "to_state": state,
                    },
                    idempotency_key=f"{event_key}:circuit",
                )
            await self._events.append(
                (
                    "provider.rate_limited"
                    if error_code == "PROVIDER_RATE_LIMITED"
                    else (
                        "provider.request_succeeded"
                        if success
                        else "provider.request_failed"
                    )
                ),
                {
                    "provider": setting.provider.value,
                    "capability": setting.capability.value,
                    "error_code": error_code,
                },
                idempotency_key=f"{event_key}:outcome",
            )
            await self._session.flush()

    @staticmethod
    def _cooldown_remaining_seconds(
        opened_at: Any,
        cooldown_index: int,
        occurred_at: datetime,
    ) -> int:
        if opened_at is None:
            return 0
        if isinstance(opened_at, datetime):
            opened_timestamp = opened_at.timestamp()
        else:
            try:
                opened_timestamp = float(opened_at)
            except (TypeError, ValueError):
                return 0
        cooldowns = (60, 180, 300)
        cooldown = cooldowns[min(max(cooldown_index, 0), len(cooldowns) - 1)]
        return max(0, int(opened_timestamp + cooldown - occurred_at.timestamp()))

    async def add_failure_sample(self, sample: ProviderFailureSample) -> None:
        self._session.add(sample)

    async def replay_mutation(
        self, idempotency_key: str, digest: str
    ) -> dict[str, Any] | None:
        async with self._session.begin():
            return await self._idempotent_replay(idempotency_key, digest)

    async def _latest_config(
        self, provider: ProviderCode
    ) -> ProviderConfigVersion | None:
        return await self._session.scalar(
            select(ProviderConfigVersion)
            .where(ProviderConfigVersion.provider_code == provider.value)
            .order_by(ProviderConfigVersion.version.desc())
            .limit(1)
        )

    async def _settings(
        self, provider: ProviderCode, version: int
    ) -> list[ProviderCapabilitySetting]:
        rows = await self._session.scalars(
            select(ProviderCapabilitySetting)
            .where(
                ProviderCapabilitySetting.provider_code == provider.value,
                ProviderCapabilitySetting.config_version == version,
            )
            .order_by(ProviderCapabilitySetting.priority)
        )
        return list(rows)

    async def _idempotent_replay(self, key: str, digest: str) -> dict[str, Any] | None:
        existing = await self._session.scalar(
            select(ProviderMutationRequest).where(
                ProviderMutationRequest.idempotency_key == key
            )
        )
        if existing is None:
            return None
        if existing.request_digest != digest:
            raise AppError(
                code="IDEMPOTENCY_KEY_CONFLICT",
                message="幂等键已用于不同请求",
                status_code=409,
            )
        return existing.response_summary

    @staticmethod
    def _route(row: ProviderCapabilitySetting) -> ProviderRouteSetting:
        return ProviderRouteSetting(
            provider=ProviderCode(row.provider_code),
            capability=ProviderCapability(row.capability),
            enabled=row.enabled,
            priority=row.priority,
            concurrency=row.concurrency,
            rate_per_second=row.rate_per_second,
            timeout_seconds=row.timeout_seconds,
            auto_switch=row.auto_switch,
        )

    @staticmethod
    def _setting_dict(row: ProviderCapabilitySetting) -> dict[str, Any]:
        return {
            "capability": row.capability,
            "enabled": row.enabled,
            "priority": row.priority,
            "concurrency": row.concurrency,
            "rate_per_second": row.rate_per_second,
            "timeout_seconds": row.timeout_seconds,
            "auto_switch": row.auto_switch,
        }

    @staticmethod
    def _circuit_dict(row: ProviderCircuitState) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "provider_code": row.provider_code,
            "capability": row.capability,
            "state": row.state,
            "consecutive_failures": row.consecutive_failures,
            "cooldown_index": row.cooldown_index,
            "opened_at": row.opened_at,
        }

    @staticmethod
    def _mutation(
        context: AuditContext,
        *,
        digest: str,
        operation: str,
        object_id: str,
        response: dict[str, Any],
    ) -> ProviderMutationRequest:
        return ProviderMutationRequest(
            idempotency_key=context.idempotency_key,
            request_digest=digest,
            operation=operation,
            object_id=object_id,
            response_summary=response,
            request_id=context.request_id,
            actor_user_id=str(context.actor_user_id),
            session_id=str(context.session_id),
            trusted_ip=str(context.trusted_ip),
        )
