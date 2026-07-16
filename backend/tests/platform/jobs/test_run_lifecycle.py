from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import (
    JobProgress,
    JobResult,
    JobRunStatus,
    JobStatus,
    SubmitJob,
)
from long_invest.platform.jobs.models import Job, JobRun
from long_invest.platform.jobs.service import JobService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def create_queued_job(database: Database) -> Job:
    unique = uuid4().hex
    async with database.transaction() as session:
        job = await JobService(session).submit(
            SubmitJob(
                job_type="RUN_LIFECYCLE_TEST",
                queue="maintenance",
                idempotency_scope=f"run:{unique}",
                idempotency_key=f"key-{unique}",
                request_id=f"req_{unique}",
                config_snapshot={},
            )
        )
        job.status = JobStatus.QUEUED
        return job


@pytest.mark.anyio
async def test_claim_start_heartbeat_progress_and_complete_are_fenced() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        job = await create_queued_job(database)
        async with database.transaction() as session:
            service = JobService(session)
            run = await service.claim(
                job_id=job.id,
                worker_id="worker-a",
            )
            assert run.soft_timeout_seconds == 30
            assert run.hard_timeout_seconds == 60
            assert await service.start(job_id=job.id, fence_token=run.fence_token)
            assert await service.heartbeat(job_id=job.id, fence_token=run.fence_token)
            assert await service.report_progress(
                job_id=job.id,
                fence_token=run.fence_token,
                progress=JobProgress(completed=2, total=5, message="处理中"),
            )
            assert await service.complete(
                job_id=job.id,
                fence_token=run.fence_token,
                result=JobResult.success_result(
                    data={"processed": 5},
                    metrics={"duration_ms": 10},
                ),
            )
            assert not await service.complete(
                job_id=job.id,
                fence_token=run.fence_token,
                result=JobResult.success_result(data={"processed": 99}),
            )

        async with database.session() as session:
            stored_job = await session.get(Job, job.id)
            stored_run = await session.scalar(
                select(JobRun).where(JobRun.id == run.id)
            )

        assert stored_job is not None
        assert stored_job.status == JobStatus.SUCCEEDED
        assert stored_job.progress == {
            "completed": 2,
            "total": 5,
            "message": "处理中",
        }
        assert stored_job.result_summary is not None
        assert stored_job.result_summary["data"] == {"processed": 5}
        assert stored_run is not None and stored_run.status == JobRunStatus.SUCCEEDED
        assert stored_run.heartbeat_at is not None
        assert stored_run.ended_at is not None
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_successful_partial_result_sets_partial_job_status() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        job = await create_queued_job(database)
        async with database.transaction() as session:
            service = JobService(session)
            run = await service.claim(job_id=job.id, worker_id="worker-partial")
            await service.start(job_id=job.id, fence_token=run.fence_token)
            await service.complete(
                job_id=job.id,
                fence_token=run.fence_token,
                result=JobResult(
                    success=True,
                    code="PARTIAL",
                    message="partial business result",
                    retryable=False,
                ),
            )

        async with database.session() as session:
            stored_job = await session.get(Job, job.id)
            assert stored_job is not None
            assert stored_job.status == JobStatus.PARTIAL
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_late_old_fence_is_superseded_and_cannot_overwrite_new_run() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        job = await create_queued_job(database)
        async with database.transaction() as session:
            first = await JobService(session).claim(
                job_id=job.id,
                worker_id="worker-old",
            )
        async with database.transaction() as session:
            stored_job = await session.get(Job, job.id, with_for_update=True)
            stored_first = await session.get(JobRun, first.id, with_for_update=True)
            assert stored_job is not None and stored_first is not None
            stored_first.status = JobRunStatus.LOST
            stored_first.ended_at = datetime.now(UTC)
            stored_job.status = JobStatus.QUEUED
            stored_job.current_run_id = None
            stored_job.current_fence_token = None
        async with database.transaction() as session:
            second = await JobService(session).claim(
                job_id=job.id,
                worker_id="worker-new",
            )
        async with database.transaction() as session:
            accepted = await JobService(session).complete(
                job_id=job.id,
                fence_token=first.fence_token,
                result=JobResult.success_result(data={"source": "old"}),
            )

        async with database.session() as session:
            stored_job = await session.get(Job, job.id)
            old_run = await session.get(JobRun, first.id)
            new_run = await session.get(JobRun, second.id)

        assert accepted is False
        assert stored_job is not None
        assert stored_job.status == JobStatus.RUNNING
        assert stored_job.current_run_id == second.id
        assert stored_job.result_summary is None
        assert old_run is not None and old_run.status == JobRunStatus.SUPERSEDED
        assert new_run is not None and new_run.status == JobRunStatus.CLAIMED
    finally:
        await database.dispose()
