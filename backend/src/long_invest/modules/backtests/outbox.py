from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.backtests.service import BacktestEvent
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class OutboxWriter(Protocol):
    async def append(self, **kwargs: Any) -> None: ...


class BacktestOutboxAdapter:
    def __init__(
        self,
        session: AsyncSession,
        writer: OutboxWriter | None = None,
        job_service_factory: Callable[[AsyncSession], Any] = JobService,
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

    async def emit(self, event: BacktestEvent) -> None:
        await self._writer.append(
            session=self._session,
            topic=event.topic,
            aggregate_type="backtest",
            aggregate_id=str(event.task_id),
            queue="domain-events",
            payload={"event_type": event.topic, **event.payload},
            dedupe_key=event.dedupe_key,
        )
        command = _job_for_event(event)
        if command is not None:
            await self._job_service_factory(self._session).submit(command)


def _job_for_event(event: BacktestEvent) -> SubmitJob | None:
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
    return SubmitJob(
        job_type="BACKTEST_SINGLE",
        queue="backtest-single",
        idempotency_scope="backtest-execution",
        idempotency_key=event.dedupe_key,
        request_id=request_id,
        config_snapshot={
            "backtest_task_id": str(event.task_id),
            "generation": generation,
            "recover": recover,
        },
        business_object_type="backtest_task",
        business_object_id=str(event.task_id),
        created_by_user_id=actor_user_id,
        soft_timeout_seconds=900,
        hard_timeout_seconds=1200,
    )
