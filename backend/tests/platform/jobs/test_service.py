from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobProgress,
    JobResult,
    JobRunStatus,
    JobStatus,
    SubmitJob,
)
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.service import JobService
from long_invest.platform.outbox.models import EventOutbox


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def submit_command(
    *,
    scope: str,
    key: str,
    sample: int = 1,
    soft_timeout_seconds: int = 30,
    hard_timeout_seconds: int = 60,
) -> SubmitJob:
    return SubmitJob(
        job_type="FOUNDATION_TEST",
        queue="maintenance",
        idempotency_scope=scope,
        idempotency_key=key,
        request_id=f"req_{uuid4().hex}",
        config_snapshot={"sample": sample},
        soft_timeout_seconds=soft_timeout_seconds,
        hard_timeout_seconds=hard_timeout_seconds,
    )


@pytest.mark.anyio
async def test_submit_writes_job_and_outbox_in_caller_transaction() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    unique = uuid4().hex
    command = submit_command(scope=f"test:{unique}", key=f"key-{unique}")
    try:
        async with database.transaction() as session:
            job = await JobService(session).submit(command)

        async with database.session() as session:
            stored_job = await session.scalar(select(Job).where(Job.id == job.id))
            stored_outbox = await session.scalar(
                select(EventOutbox).where(
                    EventOutbox.aggregate_id == str(job.id),
                    EventOutbox.topic == "jobs.dispatch",
                )
            )
            changed = await session.scalar(
                select(EventOutbox).where(
                    EventOutbox.aggregate_id == str(job.id),
                    EventOutbox.topic == "job.changed.v1",
                )
            )

        assert stored_job is not None
        assert stored_job.soft_timeout_seconds == 30
        assert stored_job.hard_timeout_seconds == 60
        assert stored_outbox is not None
        assert stored_outbox.payload == {
            "job_id": str(job.id),
            "outbox_id": str(stored_outbox.id),
            "job_type": "FOUNDATION_TEST",
            "queue": "maintenance",
            "request_id": command.request_id,
        }
        assert changed is not None
        assert changed.payload == {
            "job_id": str(job.id),
            "status": "PENDING_DISPATCH",
            "version": 1,
            "progress": {},
            "request_id": command.request_id,
            "change": "submitted",
        }
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_submit_rolls_back_job_and_outbox_together() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    unique = uuid4().hex
    command = submit_command(scope=f"test:{unique}", key=f"key-{unique}")
    job_id = None
    try:
        with pytest.raises(RuntimeError, match="force rollback"):
            async with database.transaction() as session:
                job = await JobService(session).submit(command)
                job_id = job.id
                raise RuntimeError("force rollback")

        async with database.session() as session:
            job_count = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.idempotency_scope == command.idempotency_scope)
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.aggregate_id == str(job_id))
            )

        assert job_count == 0
        assert outbox_count == 0
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_same_idempotency_content_returns_existing_job() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    unique = uuid4().hex
    first_command = submit_command(scope=f"test:{unique}", key="same-key")
    replay_command = submit_command(scope=f"test:{unique}", key="same-key")
    try:
        async with database.transaction() as session:
            first = await JobService(session).submit(first_command)
        async with database.transaction() as session:
            replay = await JobService(session).submit(replay_command)

        async with database.session() as session:
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(
                    EventOutbox.aggregate_id == str(first.id),
                    EventOutbox.topic == "jobs.dispatch",
                )
            )

        assert replay.id == first.id
        assert outbox_count == 1
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_changed_event_contains_only_safe_resource_fields() -> None:
    calls = []

    class Writer:
        async def append(self, **kwargs):
            calls.append(kwargs)

    job_id = uuid4()
    job = SimpleNamespace(
        id=job_id,
        status="RUNNING",
        version=4,
        progress={"completed": 2, "total": 5, "message": "处理中"},
        request_id="req-safe",
        config_snapshot={"provider_token": "must-not-leak"},
    )
    session = SimpleNamespace()

    await JobService(session, outbox_writer=Writer()).append_changed(
        job, change="progress"
    )

    assert calls == [
        {
            "session": session,
            "topic": "job.changed.v1",
            "aggregate_type": "job",
            "aggregate_id": str(job_id),
            "queue": "maintenance",
            "payload": {
                "job_id": str(job_id),
                "status": "RUNNING",
                "version": 4,
                "progress": {
                    "completed": 2,
                    "total": 5,
                    "message": "处理中",
                },
                "request_id": "req-safe",
                "change": "progress",
            },
            "dedupe_key": f"job-changed:{job_id}:v4:progress",
        }
    ]


@pytest.mark.anyio
async def test_heartbeat_is_silent_and_duplicate_progress_is_coalesced() -> None:
    calls = []

    class Session:
        async def flush(self):
            return None

    class Writer:
        async def append(self, **kwargs):
            calls.append(kwargs)

    now_job_id = uuid4()
    fence_token = uuid4()
    job = SimpleNamespace(
        id=now_job_id,
        status=JobStatus.RUNNING,
        version=2,
        progress={},
        request_id="req-progress",
        updated_at=None,
    )
    run = SimpleNamespace(heartbeat_at=None)
    service = JobService(Session(), outbox_writer=Writer())

    async def active_run(_job_id, _fence_token):
        return job, run

    service._active_run = active_run
    progress = JobProgress(completed=1, total=3, message="处理中")

    assert await service.heartbeat(job_id=now_job_id, fence_token=fence_token)
    assert await service.report_progress(
        job_id=now_job_id,
        fence_token=fence_token,
        progress=progress,
    )
    assert await service.report_progress(
        job_id=now_job_id,
        fence_token=fence_token,
        progress=progress,
    )

    assert job.version == 3
    assert len(calls) == 1
    assert calls[0]["payload"]["change"] == "progress"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("requested_status", "method_name", "expected_status", "change"),
    [
        (JobStatus.PAUSING, "pause_at_safe_point", JobStatus.PAUSED, "paused"),
        (
            JobStatus.CANCEL_REQUESTED,
            "cancel_at_safe_point",
            JobStatus.CANCELED,
            "canceled",
        ),
    ],
)
async def test_controlled_job_finishes_at_safe_point(
    requested_status, method_name, expected_status, change
) -> None:
    calls = []

    class Session:
        async def flush(self):
            return None

    class Writer:
        async def append(self, **kwargs):
            calls.append(kwargs)

    job_id = uuid4()
    fence_token = uuid4()
    job = SimpleNamespace(
        id=job_id,
        status=requested_status,
        version=7,
        progress={"completed": 2, "total": 5},
        result_summary=None,
        request_id="req-control",
        terminal_at=None,
        updated_at=None,
        current_run_id=uuid4(),
        current_fence_token=fence_token,
    )
    run = SimpleNamespace(
        status=JobRunStatus.RUNNING,
        ended_at=None,
        heartbeat_at=None,
        metrics=None,
    )
    service = JobService(Session(), outbox_writer=Writer())

    async def active_run(_job_id, _fence_token):
        return job, run

    service._active_run = active_run
    result = JobResult.success_result(data={"completed": 2})

    assert await getattr(service, method_name)(
        job_id=job_id,
        fence_token=fence_token,
        result=result,
    )
    assert job.status == expected_status
    assert job.current_run_id is None
    assert job.current_fence_token is None
    assert run.status == JobRunStatus.CANCELED
    assert calls[-1]["payload"]["change"] == change


@pytest.mark.anyio
async def test_different_content_reusing_idempotency_key_is_conflict() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    unique = uuid4().hex
    try:
        async with database.transaction() as session:
            await JobService(session).submit(
                submit_command(scope=f"test:{unique}", key="same-key", sample=1)
            )

        with pytest.raises(AppError) as captured:
            async with database.transaction() as session:
                await JobService(session).submit(
                    submit_command(scope=f"test:{unique}", key="same-key", sample=2)
                )

        assert captured.value.status_code == 409
        assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_different_timeout_reusing_idempotency_key_is_conflict() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    unique = uuid4().hex
    try:
        async with database.transaction() as session:
            await JobService(session).submit(
                submit_command(scope=f"test:{unique}", key="same-key")
            )

        with pytest.raises(AppError) as captured:
            async with database.transaction() as session:
                await JobService(session).submit(
                    submit_command(
                        scope=f"test:{unique}",
                        key="same-key",
                        soft_timeout_seconds=45,
                        hard_timeout_seconds=60,
                    )
                )

        assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"
    finally:
        await database.dispose()
