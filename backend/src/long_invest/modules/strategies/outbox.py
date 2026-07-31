from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.strategies.service import StrategyEvent
from long_invest.platform.jobs.contracts import SubmitPostgresJob
from long_invest.platform.jobs.postgres_service import PostgresJobService
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class OutboxWriter(Protocol):
    async def append(self, **kwargs) -> None: ...


class StrategyOutboxAdapter:
    def __init__(
        self,
        session: AsyncSession,
        writer: OutboxWriter | None = None,
        job_service_factory: Callable[[AsyncSession], Any] = PostgresJobService,
    ) -> None:
        self._session = session
        self._writer = writer or TransactionalOutboxWriter()
        self._job_service_factory = job_service_factory

    async def emit(self, event: StrategyEvent) -> None:
        command = _job_for_event(event)
        if command is not None:
            await self._job_service_factory(self._session).submit(command)
            return
        await self._writer.append(
            session=self._session,
            topic=event.topic,
            aggregate_type="strategy",
            aggregate_id=str(event.strategy_id),
            queue="domain-events",
            payload={"event_type": event.topic, **event.payload},
            dedupe_key=event.dedupe_key,
        )


def _job_for_event(event: StrategyEvent) -> SubmitPostgresJob | None:
    values = event.payload
    request_id = str(values.get("request_id") or event.dedupe_key)
    actor_user_id = str(values.get("actor_user_id") or "") or None
    if event.topic == "strategy.validation_requested":
        run_id = str(values["validation_run_id"])
        backtest_task_id = values.get("backtest_task_id")
        config = {"validation_run_id": run_id}
        if backtest_task_id is not None:
            config["backtest_task_id"] = str(backtest_task_id)
        return SubmitPostgresJob(
            job_type="STRATEGY_VALIDATE",
            module_owner="strategies",
            priority=2,
            idempotency_scope="strategy-validation-run",
            idempotency_key=event.dedupe_key,
            request_id=request_id,
            config_snapshot=config,
            business_object_type="strategy_validation_run",
            business_object_id=run_id,
            created_by_user_id=actor_user_id,
            soft_timeout_seconds=900,
            hard_timeout_seconds=1200,
            recoverable=True,
        )
    if event.topic == "strategy.publish_requested":
        run_id = str(values["run_id"])
        return SubmitPostgresJob(
            job_type="STRATEGY_PUBLISH",
            module_owner="strategies",
            priority=2,
            idempotency_scope="strategy-publish-run",
            idempotency_key=event.dedupe_key,
            request_id=request_id,
            config_snapshot={"strategy_run_id": run_id},
            business_object_type="strategy_run",
            business_object_id=run_id,
            created_by_user_id=actor_user_id,
            soft_timeout_seconds=30,
            hard_timeout_seconds=45,
            recoverable=True,
        )
    return None
