from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.providers.contracts import (
    ProviderCapability,
    ProviderCode,
    RealtimeQuote,
)
from long_invest.modules.providers.repository import (
    ProviderRepositoryPort,
    request_digest,
)
from long_invest.modules.providers.resilience import (
    ProviderRouteSetting,
    ProviderRuntimeStatePort,
)
from long_invest.modules.providers.router import ProviderRouter
from long_invest.platform.errors import AppError


class ProviderService:
    def __init__(
        self,
        router: ProviderRouter,
        providers: dict[ProviderCode, Any],
        repository: ProviderRepositoryPort,
        runtime: ProviderRuntimeStatePort,
    ) -> None:
        if repository is None or runtime is None:
            raise ValueError("provider service requires persistent and shared state")
        self._router = router
        self._providers = providers
        self._repository = repository
        self._runtime = runtime

    async def list_providers(self) -> list[dict[str, Any]]:
        codes = await self._repository.list_provider_codes()
        return [await self.get_provider(code) for code in codes]

    async def get_provider(self, provider_code: ProviderCode) -> dict[str, Any]:
        self._require(provider_code)
        return await self._repository.provider_summary(provider_code)

    async def capabilities(self, provider_code: ProviderCode) -> list[dict[str, Any]]:
        summary = await self.get_provider(provider_code)
        return summary["capabilities"]

    async def health(self, provider_code: ProviderCode) -> list[dict[str, Any]]:
        self._require(provider_code)
        return await self._repository.health(provider_code)

    async def update_settings(
        self,
        provider_code: ProviderCode,
        settings: dict[str, Any],
        *,
        expected_version: int,
        reason: str,
        audit_context: AuditContext,
    ) -> dict[str, Any]:
        provider = self._require(provider_code)
        self._require_complete_audit(audit_context)
        return await self._repository.mutate_settings(
            provider_code,
            provider.capabilities,
            settings,
            expected_version=expected_version,
            reason=reason,
            context=audit_context,
        )

    async def list_circuits(self) -> list[dict[str, Any]]:
        return await self._repository.circuits()

    async def probe_circuit(
        self, circuit_id: UUID, *, reason: str, audit_context: AuditContext
    ) -> dict[str, Any]:
        return await self._probe_and_persist(
            circuit_id,
            reason=reason,
            audit_context=audit_context,
            action_code="provider.circuit_probed",
        )

    async def reset_circuit(
        self, circuit_id: UUID, *, reason: str, audit_context: AuditContext
    ) -> dict[str, Any]:
        return await self._probe_and_persist(
            circuit_id,
            reason=reason,
            audit_context=audit_context,
            action_code="provider.circuit_reset_probed",
        )

    async def quote_diagnostics(
        self,
        symbols: tuple[str, ...],
        *,
        reason: str,
        audit_context: AuditContext,
    ) -> dict[str, Any]:
        self._require_complete_audit(audit_context)
        replay = await self._repository.replay_mutation(
            audit_context.idempotency_key,
            request_digest(
                {
                    "operation": "diagnostic",
                    "symbols": symbols,
                    "reason": reason,
                }
            ),
        )
        if replay is not None:
            return replay
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        sources: list[dict[str, Any]] = []
        by_source: dict[ProviderCode, dict[str, RealtimeQuote]] = {}
        for code in (ProviderCode.EASTMONEY, ProviderCode.SINA):
            provider = self._providers.get(code)
            if provider is None:
                continue
            try:
                result = await provider.realtime_quotes(symbols, deadline)
                by_source[code] = {item.symbol: item for item in result.items}
                sources.append(
                    {
                        "provider": code.value,
                        "items": [self._quote_dict(item) for item in result.items],
                        "failures": [
                            {**asdict(item), "provider": item.provider.value}
                            for item in result.failures
                        ],
                        "batch_error_code": result.batch_error_code,
                    }
                )
            except Exception as error:
                by_source[code] = {}
                sources.append(
                    {
                        "provider": code.value,
                        "items": [],
                        "failures": [],
                        "batch_error_code": getattr(error, "code", "PROVIDER_FAILED"),
                    }
                )
        comparisons = [
            self._compare_symbol(
                symbol,
                by_source.get(ProviderCode.EASTMONEY, {}).get(symbol),
                by_source.get(ProviderCode.SINA, {}).get(symbol),
            )
            for symbol in symbols
        ]
        response = {
            "symbols": symbols,
            "sources": sources,
            "comparisons": comparisons,
        }
        await self._repository.audit_diagnostic(
            symbols,
            response,
            reason=reason,
            context=audit_context,
        )
        return response

    async def _probe_and_persist(
        self,
        circuit_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext,
        action_code: str,
    ) -> dict[str, Any]:
        self._require_complete_audit(audit_context)
        replay = await self._repository.replay_mutation(
            audit_context.idempotency_key,
            request_digest(
                {
                    "operation": action_code,
                    "circuit_id": str(circuit_id),
                    "reason": reason,
                }
            ),
        )
        if replay is not None:
            return replay
        circuit = await self._repository.circuit(circuit_id)
        setting = ProviderRouteSetting(
            provider=ProviderCode(circuit["provider_code"]),
            capability=ProviderCapability(circuit["capability"]),
            enabled=True,
            priority=1,
            concurrency=1,
            rate_per_second=1,
            timeout_seconds=10,
            auto_switch=False,
        )
        result = await self._router.probe(
            setting, datetime.now(UTC) + timedelta(seconds=10)
        )
        snapshot = await self._runtime.circuit_snapshot(setting)
        persisted = {
            "state": snapshot["state"],
            "consecutive_failures": snapshot.get(
                "consecutive_failures", snapshot.get("failures", 0)
            ),
            "cooldown_index": snapshot.get("cooldown_index", snapshot.get("level", 0)),
            "opened_at": snapshot.get("opened_at"),
            "checked_at": result.checked_at,
            "healthy": result.healthy,
            "error_code": result.error_code,
        }
        return await self._repository.persist_probe(
            circuit_id,
            persisted,
            action_code=action_code,
            reason=reason,
            context=audit_context,
        )

    def _require(self, provider_code: ProviderCode):
        provider = self._providers.get(provider_code)
        if provider is None:
            raise AppError(
                code="PROVIDER_NOT_FOUND", message="Provider 不存在", status_code=404
            )
        return provider

    @staticmethod
    def _require_complete_audit(context: AuditContext) -> None:
        missing = [
            name
            for name in (
                "request_id",
                "idempotency_key",
                "actor_user_id",
                "session_id",
                "trusted_ip",
            )
            if not getattr(context, name, None)
        ]
        if missing:
            raise AppError(
                code="AUDIT_CONTEXT_REQUIRED",
                message="Provider 写操作缺少完整审计上下文",
                status_code=500,
                details={"missing": missing},
            )

    @staticmethod
    def _quote_dict(item: RealtimeQuote) -> dict[str, Any]:
        return {
            "symbol": item.symbol,
            "price": str(item.price),
            "open": str(item.open),
            "high": str(item.high),
            "low": str(item.low),
            "previous_close": str(item.previous_close),
            "volume": item.volume,
            "amount": str(item.amount),
            "quote_time": item.quote_time.isoformat(),
            "received_at": item.received_at.isoformat(),
            "source": item.source.value,
        }

    @classmethod
    def _compare_symbol(
        cls,
        symbol: str,
        eastmoney: RealtimeQuote | None,
        sina: RealtimeQuote | None,
    ) -> dict[str, Any]:
        fields = (
            "price",
            "open",
            "high",
            "low",
            "previous_close",
            "volume",
            "amount",
            "quote_time",
        )
        differences: dict[str, Any] = {}
        for field in fields:
            left = getattr(eastmoney, field, None)
            right = getattr(sina, field, None)
            if hasattr(left, "isoformat"):
                left = left.isoformat()
            if hasattr(right, "isoformat"):
                right = right.isoformat()
            if left is not None and field != "volume":
                left = str(left)
            if right is not None and field != "volume":
                right = str(right)
            differences[field] = {
                "EASTMONEY": left,
                "SINA": right,
                "equal": left == right and left is not None,
            }
        return {
            "symbol": symbol,
            "missing_sources": [
                code.value
                for code, item in (
                    (ProviderCode.EASTMONEY, eastmoney),
                    (ProviderCode.SINA, sina),
                )
                if item is None
            ],
            "differences": differences,
        }
