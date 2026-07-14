from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.jobs.models import Job, JobRun


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_idempotency(
        self,
        *,
        scope: str,
        key: str,
    ) -> Job | None:
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

    async def lock(self, job_id: UUID) -> Job | None:
        return await self._session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )

    async def next_attempt_no(self, job_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(JobRun.attempt_no)).where(JobRun.job_id == job_id)
        )
        return (current or 0) + 1

    async def lock_run(self, run_id: UUID) -> JobRun | None:
        return await self._session.scalar(
            select(JobRun).where(JobRun.id == run_id).with_for_update()
        )

    async def lock_run_by_fence(self, fence_token: UUID) -> JobRun | None:
        return await self._session.scalar(
            select(JobRun)
            .where(JobRun.fence_token == fence_token)
            .with_for_update()
        )
