from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.service import JobService
from long_invest.platform.outbox.models import EventOutbox


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def submit_command(*, scope: str, key: str, sample: int = 1) -> SubmitJob:
    return SubmitJob(
        job_type="FOUNDATION_TEST",
        queue="maintenance",
        idempotency_scope=scope,
        idempotency_key=key,
        request_id=f"req_{uuid4().hex}",
        config_snapshot={"sample": sample},
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

        assert stored_job is not None
        assert stored_outbox is not None
        assert stored_outbox.payload == {
            "job_id": str(job.id),
            "outbox_id": str(stored_outbox.id),
            "job_type": "FOUNDATION_TEST",
            "queue": "maintenance",
            "request_id": command.request_id,
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
                select(func.count()).select_from(Job).where(
                    Job.idempotency_scope == command.idempotency_scope
                )
            )
            outbox_count = await session.scalar(
                select(func.count()).select_from(EventOutbox).where(
                    EventOutbox.aggregate_id == str(job_id)
                )
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
                select(func.count()).select_from(EventOutbox).where(
                    EventOutbox.aggregate_id == str(first.id)
                )
            )

        assert replay.id == first.id
        assert outbox_count == 1
    finally:
        await database.dispose()


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
