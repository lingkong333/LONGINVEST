from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.backtests.service import BacktestEvent
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.jobs.contracts import SubmitPostgresJob
from long_invest.platform.jobs.postgres_service import PostgresJobService
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class OutboxWriter(Protocol):
    async def append(self, **kwargs: Any) -> None: ...


class BacktestOutboxAdapter:
    def __init__(
        self,
        session: AsyncSession,
        writer: OutboxWriter | None = None,
        job_service_factory: Callable[[AsyncSession], Any] = PostgresJobService,
        audit_factory: Callable[[AsyncSession], Any] = AuditService,
    ) -> None:
        self._session = session
        self._writer = writer or TransactionalOutboxWriter()
        self._job_service_factory = job_service_factory
        self._audit = audit_factory(session)

    async def append(self, item: AuditWrite) -> Any:
        return await self._audit.append(item)

    async def find_by_idempotency(self, key: str) -> Any | None:
        return await self._audit.find_by_idempotency(key)

    async def emit(self, event: BacktestEvent) -> UUID | None:
        command = _job_for_event(event)
        if command is not None:
            job = await self._job_service_factory(self._session).submit(command)
            return job.id
        await self._writer.append(
            session=self._session,
            topic=event.topic,
            aggregate_type="backtest",
            aggregate_id=str(event.task_id),
            queue="domain-events",
            payload={"event_type": event.topic, **event.payload},
            dedupe_key=event.dedupe_key,
        )
        return None


def _job_for_event(event: BacktestEvent) -> SubmitPostgresJob | None:
    if event.topic not in {"backtest.created", "backtest.resumed"}:
        return None
    if event.topic == "backtest.resumed" and not (
        {"generation", "execution_generation"} & event.payload.keys()
    ):
        return None
    request_id = str(event.payload.get("request_id") or event.dedupe_key)
    actor_user_id = str(event.payload.get("actor_user_id") or "") or None
    generation = int(
        event.payload.get("generation", event.payload.get("execution_generation", 1))
    )
    recover = bool(event.payload.get("recover", False))
    mode = str(event.payload.get("mode", "SINGLE"))
    is_bulk = mode in {"WATCHLIST", "MARKET"}
    config_snapshot = {
        "backtest_task_id": str(event.task_id),
        "generation": generation,
        "recover": recover,
    }
    if is_bulk:
        config_snapshot["mode"] = mode
        config_snapshot["item_keys"] = [
            str(value) for value in event.payload["item_keys"]
        ]
        config_snapshot["concurrency"] = _backtest_concurrency()
    return SubmitPostgresJob(
        job_type="BACKTEST_BATCH" if is_bulk else "BACKTEST_SINGLE",
        module_owner="backtests",
        priority=3 if is_bulk else 2,
        idempotency_scope="backtest-execution",
        idempotency_key=event.dedupe_key,
        request_id=request_id,
        config_snapshot=config_snapshot,
        business_object_type="backtest_task",
        business_object_id=str(event.task_id),
        created_by_user_id=actor_user_id,
        soft_timeout_seconds=82800 if is_bulk else 900,
        hard_timeout_seconds=86400 if is_bulk else 1200,
        recoverable=True,
    )


def _backtest_concurrency() -> int:
    try:
        value = int(os.getenv("LONGINVEST_BACKTEST_CONCURRENCY", "4"))
    except ValueError as exc:
        raise ValueError("backtest concurrency must be an integer") from exc
    if value < 1:
        raise ValueError("backtest concurrency must be positive")
    return value
