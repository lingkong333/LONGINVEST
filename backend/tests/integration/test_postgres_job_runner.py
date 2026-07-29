import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobProgress,
    JobResult,
    JobStatus,
    SubmitPostgresJob,
)
from long_invest.platform.jobs.models import Job, JobItem, JobRun
from long_invest.platform.jobs.postgres_runner import PostgresJobRunner
from long_invest.platform.jobs.postgres_service import PostgresJobService
from long_invest.platform.outbox.models import EventOutbox


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def command(
    unique: str,
    *,
    sample: int = 1,
    max_attempts: int = 1,
    recoverable: bool = False,
) -> SubmitPostgresJob:
    return SubmitPostgresJob(
        job_type="POSTGRES_FOUNDATION_TEST",
        module_owner="tests",
        idempotency_scope=f"postgres-foundation:{unique}",
        idempotency_key="job",
        request_id=f"req_{unique}",
        config_snapshot={"sample": sample},
        max_attempts=max_attempts,
        recoverable=recoverable,
    )


async def remove_job(database: Database, job_id) -> None:
    async with database.transaction() as session:
        await session.execute(delete(Job).where(Job.id == job_id))


@pytest.mark.anyio
async def test_submit_is_idempotent_without_legacy_dispatch_records() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    unique = uuid4().hex
    job_id = None
    try:
        async with database.transaction() as session:
            first = await PostgresJobService(session).submit(command(unique))
            job_id = first.id
        async with database.transaction() as session:
            replay = await PostgresJobService(session).submit(command(unique))
        assert replay.id == job_id

        with pytest.raises(AppError) as captured:
            async with database.transaction() as session:
                await PostgresJobService(session).submit(command(unique, sample=2))
        assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"

        async with database.session() as session:
            run_count = await session.scalar(
                select(func.count()).select_from(JobRun).where(JobRun.job_id == job_id)
            )
            item_count = await session.scalar(
                select(func.count())
                .select_from(JobItem)
                .where(JobItem.job_id == job_id)
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.aggregate_id == str(job_id))
            )
        assert (run_count, item_count, outbox_count) == (0, 0, 0)
    finally:
        if job_id is not None:
            await remove_job(database, job_id)
        await database.dispose()


@pytest.mark.anyio
async def test_concurrent_workers_cannot_claim_the_same_job() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    unique = uuid4().hex
    async with database.transaction() as session:
        job = await PostgresJobService(session).submit(command(unique))

    async def claim(worker_id: str):
        async with database.transaction() as session:
            return await PostgresJobService(session).claim_next(
                worker_id=worker_id,
                lease_duration=timedelta(minutes=1),
            )

    try:
        claims = await asyncio.gather(claim("worker-a"), claim("worker-b"))
        claimed_ids = [value.job_id for value in claims if value is not None]
        assert claimed_ids == [job.id]
    finally:
        await remove_job(database, job.id)
        await database.dispose()


@pytest.mark.anyio
async def test_expired_lease_recovers_once_and_rejects_late_result() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    unique = uuid4().hex
    start = datetime.now(UTC)
    async with database.transaction() as session:
        job = await PostgresJobService(session).submit(
            command(unique, recoverable=True), now=start
        )
    try:
        async with database.transaction() as session:
            first = await PostgresJobService(session).claim_next(
                worker_id="worker-old",
                lease_duration=timedelta(seconds=1),
                now=start,
            )
        assert first is not None
        after_expiry = start + timedelta(seconds=2)
        async with database.transaction() as session:
            assert await PostgresJobService(session).recover_expired(
                now=after_expiry
            ) == (1, 0)
        async with database.transaction() as session:
            second = await PostgresJobService(session).claim_next(
                worker_id="worker-new",
                lease_duration=timedelta(minutes=1),
                now=after_expiry,
            )
        assert second is not None and second.job_id == job.id
        assert second.lease_token != first.lease_token

        async with database.transaction() as session:
            accepted = await PostgresJobService(session).complete(
                job.id,
                first.lease_token,
                JobResult.success_result(),
                now=after_expiry,
            )
        assert accepted is False
    finally:
        await remove_job(database, job.id)
        await database.dispose()


@pytest.mark.anyio
async def test_pause_resume_and_running_cancel_stop_at_safe_point() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    unique = uuid4().hex
    async with database.transaction() as session:
        job = await PostgresJobService(session).submit(command(unique))
    try:
        async with database.transaction() as session:
            claim = await PostgresJobService(session).claim_next(
                worker_id="worker-control",
                lease_duration=timedelta(minutes=1),
            )
        assert claim is not None
        async with database.transaction() as session:
            running = await PostgresJobService(session).command(job.id, "pause")
            assert running.status == JobStatus.RUNNING
            assert running.pause_requested is True
        async with database.transaction() as session:
            status = await PostgresJobService(session).report_progress(
                job.id,
                claim.lease_token,
                progress=JobProgress(completed=1, total=2),
                checkpoint={"offset": 1},
                lease_duration=timedelta(minutes=1),
            )
        assert status == JobStatus.PAUSED
        async with database.transaction() as session:
            resumed = await PostgresJobService(session).command(job.id, "resume")
            assert resumed.status == JobStatus.PENDING
            assert resumed.max_attempts == 2
        async with database.transaction() as session:
            claim = await PostgresJobService(session).claim_next(
                worker_id="worker-control",
                lease_duration=timedelta(minutes=1),
            )
        assert claim is not None
        async with database.transaction() as session:
            running = await PostgresJobService(session).command(job.id, "cancel")
            assert running.status == JobStatus.RUNNING
            assert running.cancel_requested is True
        async with database.transaction() as session:
            status = await PostgresJobService(session).report_progress(
                job.id,
                claim.lease_token,
                progress=JobProgress(completed=1, total=2),
                checkpoint={"offset": 1},
                lease_duration=timedelta(minutes=1),
            )
        assert status == JobStatus.CANCELED
    finally:
        await remove_job(database, job.id)
        await database.dispose()


@pytest.mark.anyio
async def test_partial_result_is_preserved_as_terminal_status() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    unique = uuid4().hex
    async with database.transaction() as session:
        job = await PostgresJobService(session).submit(command(unique))
    try:
        async with database.transaction() as session:
            claim = await PostgresJobService(session).claim_next(
                worker_id="worker-partial",
                lease_duration=timedelta(minutes=1),
            )
        assert claim is not None
        async with database.transaction() as session:
            accepted = await PostgresJobService(session).complete(
                job.id,
                claim.lease_token,
                JobResult(
                    success=True,
                    code="PARTIAL",
                    message="部分项目失败",
                    retryable=False,
                ),
            )
        assert accepted is True
        async with database.session() as session:
            stored = await session.get(Job, job.id)
            assert stored is not None and stored.status == JobStatus.PARTIAL
    finally:
        await remove_job(database, job.id)
        await database.dispose()


@pytest.mark.anyio
async def test_retryable_failure_is_limited_and_manual_retry_is_explicit() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    unique = uuid4().hex
    async with database.transaction() as session:
        job = await PostgresJobService(session).submit(command(unique, max_attempts=2))
    failure = JobResult.failure(
        code="UPSTREAM_TIMEOUT",
        message="上游超时",
        retryable=True,
    )
    try:
        for expected in (JobStatus.PENDING, JobStatus.FAILED):
            async with database.transaction() as session:
                claim = await PostgresJobService(session).claim_next(
                    worker_id="worker-retry",
                    lease_duration=timedelta(minutes=1),
                )
            assert claim is not None
            async with database.transaction() as session:
                status = await PostgresJobService(session).fail(
                    job.id,
                    claim.lease_token,
                    failure,
                )
            assert status == expected

        async with database.transaction() as session:
            retried = await PostgresJobService(session).command(job.id, "retry")
            assert retried.status == JobStatus.PENDING
            assert retried.max_attempts == 3
    finally:
        await remove_job(database, job.id)
        await database.dispose()


@pytest.mark.anyio
async def test_runner_executes_handler_and_commits_result() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    unique = uuid4().hex
    async with database.transaction() as session:
        job = await PostgresJobService(session).submit(command(unique))

    async def handler(context):
        assert context.config == {"sample": 1}
        return JobResult.success_result(data={"done": True})

    try:
        report = await PostgresJobRunner(
            database,
            {"POSTGRES_FOUNDATION_TEST": handler},
            worker_id="runner-test",
        ).run_once()
        assert report.claimed == 1
        assert report.succeeded == 1
        async with database.session() as session:
            stored = await session.get(Job, job.id)
            assert stored is not None
            assert stored.status == JobStatus.SUCCEEDED
            assert stored.result_summary["data"] == {"done": True}
    finally:
        await remove_job(database, job.id)
        await database.dispose()
