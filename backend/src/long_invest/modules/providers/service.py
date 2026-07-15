from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.providers.contracts import ProviderCode
from long_invest.modules.providers.router import ProviderRouter
from long_invest.platform.errors import AppError


class ProviderAuditPort(Protocol):
    async def record(
        self,
        *,
        action: str,
        object_id: str,
        reason: str,
        idempotency_key: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None: ...


@dataclass(slots=True)
class ProviderSettings:
    enabled: bool = True
    priority: int = 1
    concurrency: int = 2
    rate_per_second: float = 2.0
    timeout_seconds: float = 5.0
    auto_switch: bool = True
    version: int = 1


class ProviderService:
    def __init__(
        self,
        router: ProviderRouter,
        providers: dict[ProviderCode, Any],
        audit: ProviderAuditPort | None = None,
    ) -> None:
        self._router = router
        self._providers = providers
        self._audit = audit
        self._settings = {
            code: ProviderSettings(priority=index + 1)
            for index, code in enumerate(providers)
        }
        self._circuits: dict[UUID, dict[str, Any]] = {}

    async def list_providers(self) -> list[dict[str, Any]]:
        return [await self.get_provider(code) for code in self._providers]

    async def get_provider(self, provider_code: ProviderCode) -> dict[str, Any]:
        provider = self._require(provider_code)
        return {
            "provider_code": provider_code.value,
            "capabilities": sorted(item.value for item in provider.capabilities),
            "settings": asdict(self._settings[provider_code]),
        }

    async def capabilities(self, provider_code: ProviderCode) -> list[dict[str, Any]]:
        provider = self._require(provider_code)
        return [
            {"capability": item.value, "enabled": self._settings[provider_code].enabled}
            for item in sorted(provider.capabilities, key=lambda item: item.value)
        ]

    async def health(self, provider_code: ProviderCode) -> list[dict[str, Any]]:
        provider = self._require(provider_code)
        return [
            {"capability": item.value, "status": "UNKNOWN"}
            for item in sorted(provider.capabilities, key=lambda item: item.value)
        ]

    async def update_settings(
        self,
        provider_code: ProviderCode,
        settings: dict[str, Any],
        *,
        reason: str,
        audit_context: AuditContext,
    ) -> dict[str, Any]:
        self._require(provider_code)
        current = self._settings[provider_code]
        before = asdict(current)
        for key, value in settings.items():
            if value is not None:
                setattr(current, key, value)
        current.version += 1
        after = asdict(current)
        await self._audit_event(
            "provider.config_changed",
            provider_code.value,
            reason,
            audit_context,
            before,
            after,
        )
        return after

    async def list_circuits(self) -> list[dict[str, Any]]:
        return list(self._circuits.values())

    async def probe_circuit(
        self, circuit_id: UUID, *, reason: str, audit_context: AuditContext
    ) -> dict[str, Any]:
        circuit = self._circuit(circuit_id)
        before = dict(circuit)
        circuit["state"] = "CLOSED"
        await self._audit_event(
            "provider.circuit_probed",
            str(circuit_id),
            reason,
            audit_context,
            before,
            circuit,
        )
        return circuit

    async def reset_circuit(
        self, circuit_id: UUID, *, reason: str, audit_context: AuditContext
    ) -> dict[str, Any]:
        circuit = self._circuit(circuit_id)
        before = dict(circuit)
        circuit["state"] = "HALF_OPEN"
        await self._audit_event(
            "provider.circuit_half_opened",
            str(circuit_id),
            reason,
            audit_context,
            before,
            circuit,
        )
        return circuit

    async def quote_diagnostics(
        self,
        symbols: tuple[str, ...],
        *,
        reason: str = "diagnostic",
        audit_context: AuditContext | None = None,
    ) -> dict[str, Any]:
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        sources = []
        for code in (ProviderCode.EASTMONEY, ProviderCode.SINA):
            provider = self._providers.get(code)
            if provider is None:
                continue
            try:
                result = await provider.realtime_quotes(symbols, deadline)
                sources.append(
                    {
                        "provider": code.value,
                        "items": [
                            {
                                "symbol": item.symbol,
                                "price": str(item.price),
                                "quote_time": item.quote_time.isoformat(),
                            }
                            for item in result.items
                        ],
                        "failures": [asdict(item) for item in result.failures],
                        "batch_error_code": result.batch_error_code,
                    }
                )
            except Exception as error:
                sources.append(
                    {
                        "provider": code.value,
                        "items": [],
                        "failures": [],
                        "batch_error_code": getattr(error, "code", "PROVIDER_FAILED"),
                    }
                )
        if audit_context is not None:
            await self._audit_event(
                "provider.quote_diagnostics",
                ",".join(symbols),
                reason,
                audit_context,
                None,
                {"providers": [item["provider"] for item in sources]},
            )
        return {"symbols": symbols, "sources": sources}

    def _require(self, provider_code: ProviderCode):
        provider = self._providers.get(provider_code)
        if provider is None:
            raise AppError(
                code="PROVIDER_NOT_FOUND", message="Provider 不存在", status_code=404
            )
        return provider

    def _circuit(self, circuit_id: UUID) -> dict[str, Any]:
        circuit = self._circuits.get(circuit_id)
        if circuit is None:
            raise AppError(
                code="PROVIDER_CIRCUIT_NOT_FOUND",
                message="熔断记录不存在",
                status_code=404,
            )
        return circuit

    async def _audit_event(
        self,
        action: str,
        object_id: str,
        reason: str,
        context: AuditContext,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        if self._audit is not None:
            await self._audit.record(
                action=action,
                object_id=object_id,
                reason=reason,
                idempotency_key=context.idempotency_key,
                before=before,
                after=after,
            )
