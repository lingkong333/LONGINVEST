from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import JobRunStatus, JobStatus, SubmitJob
from long_invest.platform.jobs.models import Job, JobRun
from long_invest.platform.jobs.service import JobService
from long_invest.platform.jobs.watchdog import JobsWatchdog
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def create_running_job(database: Database) -> tuple[Job, JobRun]:
    unique = uuid4().hex
    async with database.transaction() as session:
        job = await JobService(session).submit(
            SubmitJob(
                job_type="WATCHDOG_TEST",
                queue="maintenance",
                idempotency_scope=f"watchdog:{unique}",
                idempotency_key=f"key-{unique}",
                request_id=f"req_{unique}",
                config_snapshot={},
            )
        )
        job.status = JobStatus.QUEUED
    async with database.transaction() as session:
        service = JobService(session)
        run = await service.claim(
            job_id=job.id,
            worker_id="worker-a",
            soft_timeout_seconds=30,
            hard_timeout_seconds=60,
        )
        await service.start(job_id=job.id, fence_token=run.fence_token)
    return job, run


@pytest.mark.anyio
async def test_watchdog_releases_expired_outbox_lease() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        job, _ = await create_running_job(database)
        async with database.transaction() as session:
            outbox = await session.scalar(
                select(EventOutbox).where(EventOutbox.aggregate_id == str(job.id))
            )
            assert outbox is not None
            outbox.status = OutboxStatus.DISPATCHING
            outbox.locked_by = "dead-dispatcher"
            outbox.locked_at = datetime.now(UTC) - timedelta(minutes=5)

        report = await JobsWatchdog(database=database).recover_once()

        async with database.session() as session:
            outbox = await session.scalar(
                select(EventOutbox).where(EventOutbox.aggregate_id == str(job.id))
            )
        assert report.outbox_leases_released == 1
        assert outbox is not None and outbox.status == OutboxStatus.PENDING
        assert outbox.locked_at is None and outbox.locked_by is None
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_watchdog_leaves_fresh_run_untouched() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        job, run = await create_running_job(database)
        report = await JobsWatchdog(database=database).recover_once()

        async with database.session() as session:
            stored_job = await session.get(Job, job.id)
            stored_run = await session.get(JobRun, run.id)
        assert report.runs_lost == 0
        assert stored_job is not None and stored_job.status == JobStatus.RUNNING
        assert stored_run is not None and stored_run.status == JobRunStatus.RUNNING
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_watchdog_marks_stale_run_lost_and_schedules_one_recovery() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        job, first = await create_running_job(database)
        async with database.transaction() as session:
            stored_run = await session.get(JobRun, first.id, with_for_update=True)
            assert stored_run is not None
            stored_run.heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)

        watchdog = JobsWatchdog(database=database)
        first_report = await watchdog.recover_once()
        second_report = await watchdog.recover_once()

        async with database.session() as session:
            stored_job = await session.get(Job, job.id)
            runs = (
                await session.scalars(
                    select(JobRun)
                    .where(JobRun.job_id == job.id)
                    .order_by(JobRun.attempt_no)
                )
            ).all()
            recovery_count = await session.scalar(
                select(func.count()).select_from(EventOutbox).where(
                    EventOutbox.dedupe_key == f"job-recovery:{first.id}"
                )
            )

        assert first_report.runs_lost == 1
        assert first_report.recoveries_scheduled == 1
        assert second_report.runs_lost == 0
        assert second_report.recoveries_scheduled == 0
        assert len(runs) == 2
        assert runs[0].status == JobRunStatus.LOST
        assert runs[1].status == JobRunStatus.CLAIMED
        assert runs[1].attempt_no == 2
        assert stored_job is not None
        assert stored_job.status == JobStatus.WAITING_RETRY
        assert stored_job.current_run_id == runs[1].id
        assert stored_job.current_fence_token == runs[1].fence_token
        assert recovery_count == 1
    finally:
        await database.dispose()
