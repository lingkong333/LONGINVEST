from __future__ import annotations

from datetime import datetime
from uuid import UUID

from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.admin import (
    JobAction,
    JobAdminService,
    JobCommandContext,
)
from long_invest.platform.jobs.contracts import JobItemStatus, JobStatus


class JobAdminApplication:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_jobs(
        self,
        *,
        page: int,
        page_size: int,
        status: JobStatus | None = None,
        job_type: str | None = None,
        queue: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ):
        async with self._database.session() as session:
            return await JobAdminService(session).list_jobs(
                page=page,
                page_size=page_size,
                status=status,
                job_type=job_type,
                queue=queue,
                created_from=created_from,
                created_to=created_to,
            )

    async def get_job(self, job_id: UUID):
        async with self._database.session() as session:
            return await JobAdminService(session).get_job(job_id)

    async def list_runs(self, job_id: UUID):
        async with self._database.session() as session:
            return await JobAdminService(session).list_runs(job_id)

    async def list_items(
        self,
        job_id: UUID,
        *,
        page: int,
        page_size: int,
        status: JobItemStatus | None = None,
    ):
        async with self._database.session() as session:
            return await JobAdminService(session).list_items(
                job_id, page=page, page_size=page_size, status=status
            )

    async def allowed_actions(self, job_id: UUID) -> tuple[str, ...]:
        async with self._database.session() as session:
            return await JobAdminService(session).allowed_actions(job_id)

    async def command(
        self,
        job_id: UUID,
        action: JobAction,
        context: JobCommandContext,
    ):
        async with self._database.transaction() as session:
            return await JobAdminService(session).command(job_id, action, context)
