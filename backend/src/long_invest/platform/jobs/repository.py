from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.jobs.models import Job, JobItem, JobRun


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: UUID) -> Job | None:
        return await self._session.get(Job, job_id)

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
            select(JobRun).where(JobRun.fence_token == fence_token).with_for_update()
        )

    async def item_keys(self, job_id: UUID) -> set[str]:
        result = await self._session.scalars(
            select(JobItem.item_key).where(JobItem.job_id == job_id)
        )
        return set(result.all())

    async def lock_item(self, job_id: UUID, item_key: str) -> JobItem | None:
        return await self._session.scalar(
            select(JobItem)
            .where(JobItem.job_id == job_id, JobItem.item_key == item_key)
            .with_for_update()
        )

    async def item_progress(self, job_id: UUID) -> tuple[int, int]:
        total = await self._session.scalar(
            select(func.count()).select_from(JobItem).where(JobItem.job_id == job_id)
        )
        completed = await self._session.scalar(
            select(func.count())
            .select_from(JobItem)
            .where(
                JobItem.job_id == job_id,
                JobItem.status.in_(("SUCCEEDED", "FAILED", "SKIPPED", "CANCELED")),
            )
        )
        return int(completed or 0), int(total or 0)

    async def recover_active_items(self, job_id: UUID) -> None:
        await self._session.execute(
            update(JobItem)
            .where(
                JobItem.job_id == job_id,
                JobItem.status.in_(("FETCHING", "VALIDATING", "RUNNING", "SAVING")),
            )
            .values(status="PENDING", started_at=None, ended_at=None)
        )
        await self._session.flush()

    async def claim_pending_items(self, job_id: UUID, *, limit: int) -> list[JobItem]:
        items = list(
            (
                await self._session.scalars(
                    select(JobItem)
                    .where(JobItem.job_id == job_id, JobItem.status == "PENDING")
                    .order_by(JobItem.item_key)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        return items

    async def item_status_counts(self, job_id: UUID) -> dict[str, int]:
        rows = (
            await self._session.execute(
                select(JobItem.status, func.count())
                .where(JobItem.job_id == job_id)
                .group_by(JobItem.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}

    async def cancel_pending_items(self, job_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(JobItem)
            .where(JobItem.job_id == job_id, JobItem.status == "PENDING")
            .values(status="CANCELED", ended_at=now, updated_at=now)
        )
        await self._session.flush()
