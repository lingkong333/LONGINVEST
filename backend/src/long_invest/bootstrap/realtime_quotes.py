from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import structlog

from long_invest.bootstrap.providers import (
    ProviderAuditAdapter,
    ProviderEventAdapter,
    get_provider_resources,
)
from long_invest.modules.providers.contracts import ProviderCapability
from long_invest.modules.providers.repository import ProviderRepository
from long_invest.modules.providers.resilience import ProviderRoutePlan
from long_invest.modules.providers.router import ProviderRouter
from long_invest.modules.quotes.collection import InMemoryQuoteCollector
from long_invest.modules.quotes.contracts import (
    RealtimeBatchResult,
    RealtimeBatchStatus,
    RealtimeCheckMode,
)
from long_invest.modules.signals.application import SignalApplication
from long_invest.modules.signals.contracts import EvaluationReason
from long_invest.platform.database.engine import Database, get_database

logger = structlog.get_logger(__name__)


class FrozenProviderConfiguration:
    def __init__(self, plan: ProviderRoutePlan) -> None:
        self._plan = plan

    async def route_plan(self, capability: ProviderCapability) -> ProviderRoutePlan:
        if capability is not self._plan.capability:
            return ProviderRoutePlan(capability, ())
        return self._plan


class DatabaseProviderObserver:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def record_half_open(self, setting: Any, **values: Any) -> None:
        await self._record("record_half_open", setting, values)

    async def record_outcome(self, setting: Any, **values: Any) -> None:
        await self._record("record_outcome", setting, values)

    async def _record(
        self, method: str, setting: Any, values: dict[str, Any]
    ) -> None:
        async with self._database.transaction() as session:
            repository = ProviderRepository(
                session,
                audit=ProviderAuditAdapter(session),
                events=ProviderEventAdapter(session),
            )
            await getattr(repository, method)(setting, **values)


class RealtimeQuoteRuntime:
    def __init__(
        self,
        database: Database,
        *,
        signals: SignalApplication | None = None,
        now=None,
    ) -> None:
        self._database = database
        self._signals = signals or SignalApplication(database)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    async def run(
        self,
        *,
        symbols: tuple[str, ...],
        scheduled_at: datetime,
        mode: RealtimeCheckMode,
        evaluate_signals: bool,
        expected_subscription_versions: dict[str, int] | None = None,
        timeout_seconds: int = 30,
        operation_key: str,
    ) -> RealtimeBatchResult:
        unique_symbols = tuple(dict.fromkeys(symbols))
        if not unique_symbols:
            raise ValueError("realtime quote scope cannot be empty")
        if self._lock.locked():
            now = self._now()
            logger.warning(
                "realtime_quote_batch_overlap_skipped",
                category="scheduler",
                scheduled_at=scheduled_at.isoformat(),
                expected_count=len(unique_symbols),
            )
            return RealtimeBatchResult(
                status=RealtimeBatchStatus.OVERLAP_SKIPPED,
                mode=mode,
                scheduled_at=scheduled_at,
                started_at=now,
                completed_at=now,
                expected_symbols=unique_symbols,
                quotes=(),
                failures=(),
            )

        async with self._lock:
            prepared = (
                await self._signals.prepare_realtime(
                    unique_symbols,
                    expected_subscription_versions=expected_subscription_versions,
                )
                if evaluate_signals
                else ()
            )
            result = await InMemoryQuoteCollector(
                await self._provider_router(), now=self._now
            ).collect(
                symbols=unique_symbols,
                scheduled_at=scheduled_at,
                timeout_seconds=timeout_seconds,
                mode=mode,
            )
            succeeded = 0
            failed = 0
            if evaluate_signals:
                quote_by_symbol = {item.symbol: item for item in result.quotes}
                for preparation in prepared:
                    quote = quote_by_symbol.get(preparation.symbol)
                    if quote is None:
                        continue
                    try:
                        await self._signals.evaluate_realtime(
                            preparation,
                            quote,
                            scheduled_at=scheduled_at,
                            reason=(
                                EvaluationReason.SCHEDULED_QUOTE
                                if mode is RealtimeCheckMode.SCHEDULED
                                else EvaluationReason.MANUAL_CHECK
                            ),
                            request_id=_request_id(operation_key),
                            idempotency_key=(
                                f"realtime:{preparation.subscription_id}:"
                                f"{_operation_digest(operation_key)}:"
                                f"{preparation.target_version}"
                            ),
                        )
                    except Exception:
                        failed += 1
                        logger.exception(
                            "realtime_quote_signal_commit_failed",
                            category="scheduler",
                            symbol=preparation.symbol,
                            scheduled_at=scheduled_at.isoformat(),
                        )
                    else:
                        succeeded += 1
            result = replace(
                result,
                signal_succeeded=succeeded,
                signal_failed=failed,
            )
            if result.failures or failed:
                logger.warning(
                    "realtime_quote_batch_partial",
                    category="scheduler",
                    scheduled_at=scheduled_at.isoformat(),
                    expected_count=result.expected_count,
                    valid_count=result.valid_count,
                    signal_failed=failed,
                    failures=[
                        {"symbol": item.symbol, "code": item.code}
                        for item in result.failures
                    ],
                )
            else:
                logger.info(
                    "realtime_quote_batch_completed",
                    category="scheduler",
                    scheduled_at=scheduled_at.isoformat(),
                    expected_count=result.expected_count,
                    signal_succeeded=succeeded,
                )
            return result

    async def _provider_router(self) -> ProviderRouter:
        async with self._database.session() as session:
            repository = ProviderRepository(
                session,
                audit=ProviderAuditAdapter(session),
                events=ProviderEventAdapter(session),
            )
            plan = await repository.route_plan(
                ProviderCapability.REALTIME_QUOTE_BATCH
            )
        resources = get_provider_resources()
        return ProviderRouter(
            providers=resources.providers,
            config=FrozenProviderConfiguration(plan),
            runtime=resources.runtime,
            observer=DatabaseProviderObserver(self._database),
        )


_runtime: RealtimeQuoteRuntime | None = None


def get_realtime_quote_runtime() -> RealtimeQuoteRuntime:
    global _runtime
    if _runtime is None:
        _runtime = RealtimeQuoteRuntime(get_database())
    return _runtime


def _request_id(operation_key: str) -> str:
    normalized = "".join(
        character for character in operation_key if character.isalnum()
    )
    return f"quote-{normalized[-56:]}"


def _operation_digest(operation_key: str) -> str:
    return hashlib.sha256(operation_key.encode()).hexdigest()
