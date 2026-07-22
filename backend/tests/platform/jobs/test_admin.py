from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.admin import JobAdminService, JobCommandContext
from long_invest.platform.jobs.contracts import (
    JobItemStatus,
    JobRunStatus,
    JobStatus,
)
from long_invest.platform.jobs.models import Job, JobItem, JobRun
from long_invest.platform.outbox.models import EventOutbox


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _job(*, status: JobStatus, suffix: str) -> Job:
    return Job(
        id=uuid4(),
        job_type="ADMIN_TEST",
        queue="maintenance",
        status=status,
        config_snapshot={"sample": suffix},
        idempotency_scope=f"admin-test:{suffix}",
        idempotency_key=f"create:{suffix}",
        request_hash=uuid4().hex,
        request_id=f"req-{suffix}",
        soft_timeout_seconds=30,
        hard_timeout_seconds=60,
    )


def _context(key: str, *, version: int = 1) -> JobCommandContext:
    return JobCommandContext(
        request_id=f"req-{key}",
        idempotency_key=key,
        actor_user_id="admin-test",
        reason="测试任务控制",
        expected_version=version,
    )


@pytest.mark.anyio
async def test_list_jobs_returns_empty_page_for_unmatched_filter() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        async with database.session() as session:
            page = await JobAdminService(session).list_jobs(
                page=1, page_size=10, job_type=f"missing-{uuid4().hex}"
            )

        assert page.items == ()
        assert page.total == 0
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_cancel_is_idempotent_and_audit_outbox_are_atomic() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    suffix = uuid4().hex
    job = _job(status=JobStatus.QUEUED, suffix=suffix)
    key = f"cancel-{suffix}"
    try:
        async with database.transaction() as session:
            session.add(job)
        async with database.transaction() as session:
            first = await JobAdminService(session).command(
                job.id, "cancel", _context(key)
            )
        async with database.transaction() as session:
            replay = await JobAdminService(session).command(
                job.id, "cancel", _context(key)
            )
        async with database.session() as session:
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.object_id == str(job.id),
                    AuditEvent.action_code == "JOB_CANCEL",
                )
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(
                    EventOutbox.aggregate_id == str(job.id),
                    EventOutbox.topic == "jobs.control",
                )
            )

        assert first.status == JobStatus.CANCELED
        assert replay.status == JobStatus.CANCELED
        assert first.version == 2
        assert replay.version == 2
        assert audit_count == 1
        assert outbox_count == 1
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_pause_rejects_stale_version_and_illegal_status() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    suffix = uuid4().hex
    job = _job(status=JobStatus.QUEUED, suffix=suffix)
    try:
        async with database.transaction() as session:
            session.add(job)
        with pytest.raises(AppError) as stale:
            async with database.transaction() as session:
                await JobAdminService(session).command(
                    job.id, "pause", _context(f"stale-{suffix}", version=2)
                )
        with pytest.raises(AppError) as illegal:
            async with database.transaction() as session:
                await JobAdminService(session).command(
                    job.id, "pause", _context(f"illegal-{suffix}")
                )

        assert stale.value.code == "JOB_VERSION_CONFLICT"
        assert illegal.value.code == "JOB_ACTION_NOT_ALLOWED"
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_retry_creates_new_run_and_preserves_failed_run() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    suffix = uuid4().hex
    job = _job(status=JobStatus.FAILED, suffix=suffix)
    old_run = JobRun(
        job_id=job.id,
        attempt_no=1,
        fence_token=uuid4(),
        status=JobRunStatus.FAILED,
        soft_timeout_seconds=30,
        hard_timeout_seconds=60,
        error_code="TEMPORARY_FAILURE",
    )
    try:
        async with database.transaction() as session:
            session.add_all((job, old_run))
        async with database.transaction() as session:
            retried = await JobAdminService(session).command(
                job.id, "retry", _context(f"retry-{suffix}")
            )
        async with database.session() as session:
            runs = (
                await session.scalars(
                    select(JobRun)
                    .where(JobRun.job_id == job.id)
                    .order_by(JobRun.attempt_no)
                )
            ).all()
            dispatch = await session.scalar(
                select(EventOutbox).where(
                    EventOutbox.aggregate_id == str(job.id),
                    EventOutbox.topic == "jobs.dispatch",
                )
            )

        assert retried.status == JobStatus.PENDING_DISPATCH
        assert [run.status for run in runs] == [
            JobRunStatus.FAILED,
            JobRunStatus.CLAIMED,
        ]
        assert runs[0].error_code == "TEMPORARY_FAILURE"
        assert retried.current_run_id == runs[1].id
        assert dispatch is not None
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_resume_closes_paused_run_before_creating_next_run() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    suffix = uuid4().hex
    job = _job(status=JobStatus.PAUSED, suffix=suffix)
    job.queue = "bulk-history"
    previous = JobRun(
        job_id=job.id,
        attempt_no=1,
        fence_token=uuid4(),
        status=JobRunStatus.RUNNING,
        soft_timeout_seconds=30,
        hard_timeout_seconds=60,
    )
    try:
        async with database.transaction() as session:
            session.add(job)
        async with database.transaction() as session:
            session.add(previous)
            await session.flush()
            stored_job = await session.get(Job, job.id, with_for_update=True)
            stored_job.current_run_id = previous.id
            stored_job.current_fence_token = previous.fence_token
        async with database.transaction() as session:
            resumed = await JobAdminService(session).command(
                job.id, "resume", _context(f"resume-{suffix}")
            )
        async with database.session() as session:
            old_run = await session.get(JobRun, previous.id)
            next_run = await session.get(JobRun, resumed.current_run_id)

        assert old_run is not None
        assert old_run.status == JobRunStatus.CANCELED
        assert old_run.exit_type == "RESUME"
        assert next_run is not None
        assert next_run.attempt_no == 2
        assert resumed.current_fence_token == next_run.fence_token
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_retry_failed_items_does_not_change_successful_items() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    suffix = uuid4().hex
    job = _job(status=JobStatus.PARTIAL, suffix=suffix)
    failed = JobItem(
        job_id=job.id,
        item_key="failed",
        status=JobItemStatus.FAILED,
        attempt_count=1,
        error_code="ITEM_FAILED",
    )
    succeeded = JobItem(
        job_id=job.id,
        item_key="succeeded",
        status=JobItemStatus.SUCCEEDED,
        attempt_count=1,
        result_ref={"saved": True},
    )
    try:
        async with database.transaction() as session:
            session.add_all((job, failed, succeeded))
        async with database.transaction() as session:
            await JobAdminService(session).command(
                job.id,
                "retry-failed-items",
                _context(f"retry-items-{suffix}"),
            )
        async with database.session() as session:
            stored_failed = await session.get(JobItem, failed.id)
            stored_succeeded = await session.get(JobItem, succeeded.id)

        assert stored_failed is not None
        assert stored_failed.status == JobItemStatus.PENDING
        assert stored_failed.error_code is None
        assert stored_succeeded is not None
        assert stored_succeeded.status == JobItemStatus.SUCCEEDED
        assert stored_succeeded.result_ref == {"saved": True}
    finally:
        await database.dispose()
