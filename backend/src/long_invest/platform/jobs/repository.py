from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.jobs.models import Job


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
