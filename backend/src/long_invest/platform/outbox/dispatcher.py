from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import structlog

from long_invest.platform.database.engine import Database
from long_invest.platform.outbox.repository import ClaimedOutbox, OutboxRepository

logger = structlog.get_logger(__name__)


class QueuePublisher(Protocol):
    async def publish(
        self,
        *,
        queue: str,
        outbox_id: UUID,
        job_id: UUID,
        timeout_seconds: int,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class DispatchReport:
    claimed: int
    dispatched: int
    failed: int


class OutboxDispatcher:
    def __init__(
        self,
        *,
        database: Database,
        publisher: QueuePublisher,
        dispatcher_id: str,
        batch_size: int = 50,
    ) -> None:
        self._database = database
        self._publisher = publisher
        self._dispatcher_id = dispatcher_id
        self._batch_size = batch_size

    async def dispatch_once(self) -> DispatchReport:
        claimed = await self._claim()
        dispatched = 0
        failed = 0
        for event in claimed:
            try:
                rq_job_id = await self._publisher.publish(
                    queue=event.queue,
                    outbox_id=event.id,
                    job_id=event.job_id,
                    timeout_seconds=event.hard_timeout_seconds,
                )
            except Exception as exc:
                failed += 1
                await self._mark_failed(event, exc)
            else:
                if await self._mark_dispatched(event, rq_job_id):
                    dispatched += 1
        return DispatchReport(
            claimed=len(claimed),
            dispatched=dispatched,
            failed=failed,
        )

    async def _claim(self) -> tuple[ClaimedOutbox, ...]:
        async with self._database.transaction() as session:
            return await OutboxRepository(session).claim_due(
                dispatcher_id=self._dispatcher_id,
                limit=self._batch_size,
            )

    async def _mark_dispatched(
        self,
        event: ClaimedOutbox,
        rq_job_id: str,
    ) -> bool:
        async with self._database.transaction() as session:
            return await OutboxRepository(session).mark_dispatched(
                outbox_id=event.id,
                dispatcher_id=self._dispatcher_id,
                rq_job_id=rq_job_id,
            )

    async def _mark_failed(self, event: ClaimedOutbox, exc: Exception) -> None:
        logger.warning(
            "outbox_dispatch_failed",
            category="maintenance",
            outbox_id=str(event.id),
            job_id=str(event.job_id),
            error_type=type(exc).__name__,
        )
        async with self._database.transaction() as session:
            await OutboxRepository(session).mark_failed(
                outbox_id=event.id,
                dispatcher_id=self._dispatcher_id,
                error_code="QUEUE_UNAVAILABLE",
                error_summary="队列暂时不可用",
            )
