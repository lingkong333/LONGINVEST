from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.jobs.contracts import JobStatus
from long_invest.platform.jobs.models import Job


class PostgresJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: UUID) -> Job | None:
        return await self._session.get(Job, job_id)

    async def lock(self, job_id: UUID) -> Job | None:
        return await self._session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )

    async def find_by_idempotency(self, scope: str, key: str) -> Job | None:
        return await self._session.scalar(
            select(Job).where(
                Job.idempotency_scope == scope,
                Job.idempotency_key == key,
            )
        )

    async def add(self, job: Job) -> Job:
        self._session.add(job)
        await self._session.flush()
        return job

    async def claim_next(
        self, now: datetime, *, job_types: tuple[str, ...] | None = None
    ) -> Job | None:
        statement = select(Job).where(
            Job.status == JobStatus.PENDING,
            Job.next_run_at <= now,
        )
        if job_types is not None:
            if not job_types:
                return None
            statement = statement.where(Job.job_type.in_(job_types))
        return await self._session.scalar(
            statement
            .order_by(Job.priority, Job.next_run_at, Job.created_at, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    async def lock_expired(self, now: datetime, *, limit: int) -> tuple[Job, ...]:
        rows = await self._session.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at <= now,
            )
            .order_by(Job.lease_expires_at, Job.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return tuple(rows.all())
