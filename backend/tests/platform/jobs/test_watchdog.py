from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs import watchdog as watchdog_module
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
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.dedupe_key == f"job-recovery:{first.id}")
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


@pytest.mark.anyio
async def test_watchdog_locks_job_before_run_and_rechecks_staleness(
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    job_id = uuid4()
    run_id = uuid4()
    fence_token = uuid4()
    candidate = SimpleNamespace(
        id=run_id,
        job_id=job_id,
        fence_token=fence_token,
        status=JobRunStatus.RUNNING,
        heartbeat_at=now - timedelta(minutes=5),
        started_at=now - timedelta(minutes=5),
        claimed_at=now - timedelta(minutes=5),
        ended_at=None,
    )
    job = SimpleNamespace(
        id=job_id,
        current_run_id=run_id,
        current_fence_token=fence_token,
    )
    calls = []

    class Result:
        def all(self):
            return [candidate]

    class Session:
        async def scalars(self, statement):
            if statement._for_update_arg is not None:
                calls.append("candidate-query-for-update")
            return Result()

        async def flush(self):
            return None

        async def scalar(self, _statement):
            return 1

        async def refresh(self, run):
            calls.append("refresh")
            run.heartbeat_at = now

    class Repository:
        def __init__(self, _session):
            pass

        async def lock(self, requested_job_id):
            assert requested_job_id == job_id
            calls.append("job")
            return job

        async def lock_run(self, requested_run_id):
            assert requested_run_id == run_id
            calls.append("run")
            return candidate

    monkeypatch.setattr(watchdog_module, "JobRepository", Repository)
    watchdog = JobsWatchdog(database=SimpleNamespace())

    async def no_recovery(*_args):
        return None

    monkeypatch.setattr(watchdog, "_schedule_recovery", no_recovery)

    lost, scheduled = await watchdog._recover_stale_runs(Session(), now)

    assert calls == ["job", "run", "refresh"]
    assert (lost, scheduled) == (0, 0)
    assert candidate.status == JobRunStatus.RUNNING


@pytest.mark.anyio
@pytest.mark.parametrize(
    "terminal_status",
    [
        JobStatus.SUCCEEDED,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.TIMED_OUT,
        JobStatus.LOST,
        JobStatus.CANCELED,
        JobStatus.REJECTED,
    ],
)
async def test_watchdog_closes_stale_run_without_overwriting_terminal_job(
    monkeypatch, terminal_status
) -> None:
    now = datetime.now(UTC)
    terminal_at = now - timedelta(seconds=10)
    updated_at = now - timedelta(seconds=5)
    job_id = uuid4()
    parent_job_id = uuid4()
    run_id = uuid4()
    fence_token = uuid4()
    run = SimpleNamespace(
        id=run_id,
        job_id=job_id,
        fence_token=fence_token,
        status=JobRunStatus.RUNNING,
        heartbeat_at=now - timedelta(minutes=5),
        started_at=now - timedelta(minutes=5),
        claimed_at=now - timedelta(minutes=5),
        ended_at=None,
        exit_type=None,
    )
    result_summary = {"success": terminal_status is not JobStatus.FAILED}
    job = SimpleNamespace(
        id=job_id,
        current_run_id=run_id,
        current_fence_token=fence_token,
        config_snapshot={"linked_parent_job_id": str(parent_job_id)},
        status=terminal_status,
        result_summary=result_summary,
        terminal_at=terminal_at,
        updated_at=updated_at,
        version=7,
    )
    parent_results = []

    class Result:
        def all(self):
            return [run]

    class Session:
        async def scalars(self, _statement):
            return Result()

        async def scalar(self, _statement):
            return 2

        async def flush(self):
            return None

        async def refresh(self, _run):
            return None

    class Repository:
        def __init__(self, _session):
            pass

        async def lock(self, _job_id):
            return job

        async def lock_run(self, _run_id):
            return run

    class Service:
        def __init__(self, _session):
            pass

        async def finalize_parent(self, requested_parent_id, result):
            parent_results.append((requested_parent_id, result.code))
            return True

    monkeypatch.setattr(watchdog_module, "JobRepository", Repository)
    monkeypatch.setattr(watchdog_module, "JobService", Service)

    lost, scheduled = await JobsWatchdog(
        database=SimpleNamespace()
    )._recover_stale_runs(Session(), now)

    assert (lost, scheduled) == (0, 0)
    assert run.status is JobRunStatus.SUPERSEDED
    assert run.ended_at == now
    assert job.status is terminal_status
    assert job.result_summary is result_summary
    assert job.terminal_at == terminal_at
    assert job.updated_at == updated_at
    assert job.version == 7
    assert job.current_run_id is None
    assert job.current_fence_token is None
    assert parent_results == []


@pytest.mark.anyio
async def test_watchdog_closes_linked_parent_after_second_lost_run(monkeypatch) -> None:
    now = datetime.now(UTC)
    job_id = uuid4()
    parent_job_id = uuid4()
    run_id = uuid4()
    fence_token = uuid4()
    run = SimpleNamespace(
        id=run_id,
        job_id=job_id,
        fence_token=fence_token,
        status=JobRunStatus.RUNNING,
        heartbeat_at=now - timedelta(minutes=5),
        started_at=now - timedelta(minutes=5),
        claimed_at=now - timedelta(minutes=5),
        ended_at=None,
        exit_type=None,
    )
    job = SimpleNamespace(
        id=job_id,
        current_run_id=run_id,
        current_fence_token=fence_token,
        config_snapshot={"linked_parent_job_id": str(parent_job_id)},
        status=JobStatus.RUNNING,
        terminal_at=None,
        updated_at=None,
        version=1,
    )
    parent_results = []

    class Result:
        def all(self):
            return [run]

    class Session:
        async def scalars(self, _statement):
            return Result()

        async def scalar(self, _statement):
            return 2

        async def flush(self):
            return None

        async def refresh(self, _run):
            return None

    class Repository:
        def __init__(self, _session):
            pass

        async def lock(self, _job_id):
            return job

        async def lock_run(self, _run_id):
            return run

    class Service:
        def __init__(self, _session):
            pass

        async def finalize_parent(self, requested_parent_id, result):
            parent_results.append((requested_parent_id, result.code))
            return True

    monkeypatch.setattr(watchdog_module, "JobRepository", Repository)
    monkeypatch.setattr(watchdog_module, "JobService", Service)

    lost, scheduled = await JobsWatchdog(
        database=SimpleNamespace()
    )._recover_stale_runs(Session(), now)

    assert (lost, scheduled) == (1, 0)
    assert job.status == JobStatus.LOST
    assert parent_results == [(parent_job_id, "JOB_HEARTBEAT_LOST")]
