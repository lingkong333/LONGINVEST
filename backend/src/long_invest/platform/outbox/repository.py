from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.jobs.contracts import JobStatus
from long_invest.platform.jobs.models import Job
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


@dataclass(frozen=True, slots=True)
class ClaimedOutbox:
    id: UUID
    job_id: UUID
    queue: str
    attempt_count: int


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: EventOutbox) -> EventOutbox:
        self._session.add(event)
        await self._session.flush()
        return event

    async def claim_due(
        self,
        *,
        dispatcher_id: str,
        limit: int,
    ) -> tuple[ClaimedOutbox, ...]:
        rows = (
            await self._session.scalars(
                select(EventOutbox)
                .where(
                    EventOutbox.status == OutboxStatus.PENDING,
                    EventOutbox.next_attempt_at <= datetime.now(UTC),
                )
                .order_by(EventOutbox.created_at, EventOutbox.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        claimed: list[ClaimedOutbox] = []
        now = datetime.now(UTC)
        for row in rows:
            row.status = OutboxStatus.DISPATCHING
            row.locked_at = now
            row.locked_by = dispatcher_id
            row.attempt_count += 1
            claimed.append(
                ClaimedOutbox(
                    id=row.id,
                    job_id=UUID(row.aggregate_id),
                    queue=row.queue,
                    attempt_count=row.attempt_count,
                )
            )
        await self._session.flush()
        return tuple(claimed)

    async def mark_dispatched(
        self,
        *,
        outbox_id: UUID,
        dispatcher_id: str,
        rq_job_id: str,
    ) -> bool:
        event = await self._locked_owned_event(outbox_id, dispatcher_id)
        if event is None:
            return False
        job = await self._session.get(
            Job,
            UUID(event.aggregate_id),
            with_for_update=True,
        )
        if job is None:
            event.status = OutboxStatus.DEAD
            event.last_error_code = "JOB_NOT_FOUND"
            event.last_error_summary = "对应任务不存在"
            return False
        now = datetime.now(UTC)
        event.status = OutboxStatus.DISPATCHED
        event.dispatched_at = now
        event.rq_job_id = rq_job_id
        event.locked_at = None
        event.locked_by = None
        event.last_error_code = None
        event.last_error_summary = None
        job.status = JobStatus.QUEUED
        job.updated_at = now
        job.version += 1
        await self._session.flush()
        return True

    async def mark_failed(
        self,
        *,
        outbox_id: UUID,
        dispatcher_id: str,
        error_code: str,
        error_summary: str,
    ) -> bool:
        event = await self._locked_owned_event(outbox_id, dispatcher_id)
        if event is None:
            return False
        delay_seconds = min(300, 2 ** min(event.attempt_count, 8))
        event.status = OutboxStatus.PENDING
        event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        event.locked_at = None
        event.locked_by = None
        event.last_error_code = error_code
        event.last_error_summary = error_summary[:500]
        await self._session.flush()
        return True

    async def _locked_owned_event(
        self,
        outbox_id: UUID,
        dispatcher_id: str,
    ) -> EventOutbox | None:
        event = await self._session.scalar(
            select(EventOutbox)
            .where(EventOutbox.id == outbox_id)
            .with_for_update()
        )
        if (
            event is None
            or event.status != OutboxStatus.DISPATCHING
            or event.locked_by != dispatcher_id
        ):
            return None
        return event
