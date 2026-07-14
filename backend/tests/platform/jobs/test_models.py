from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import JobItemStatus, JobRunStatus, JobStatus
from long_invest.platform.jobs.models import Job, JobItem, JobRun
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def new_job(*, scope: str, key: str) -> Job:
    return Job(
        job_type="FOUNDATION_TEST",
        queue="maintenance",
        priority=0,
        status=JobStatus.PENDING_DISPATCH,
        config_snapshot={"sample": True},
        idempotency_scope=scope,
        idempotency_key=key,
        request_hash="a" * 64,
        request_id=f"req_{uuid4().hex}",
    )


@pytest.mark.anyio
async def test_job_run_item_and_outbox_persist_together() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    unique = uuid4().hex
    try:
        async with database.transaction() as session:
            job = new_job(scope=f"test:{unique}", key=f"key-{unique}")
            session.add(job)
            await session.flush()
            run = JobRun(
                job_id=job.id,
                attempt_no=1,
                fence_token=uuid4(),
                status=JobRunStatus.CLAIMED,
                soft_timeout_seconds=30,
                hard_timeout_seconds=60,
            )
            item = JobItem(
                job_id=job.id,
                item_key="600000.SH",
                status=JobItemStatus.PENDING,
            )
            outbox = EventOutbox(
                topic="jobs.dispatch",
                aggregate_type="job",
                aggregate_id=str(job.id),
                queue="maintenance",
                payload={"job_id": str(job.id)},
                dedupe_key=f"job:{job.id}:dispatch",
                status=OutboxStatus.PENDING,
            )
            session.add_all((run, item, outbox))

        async with database.session() as session:
            stored = await session.scalar(select(Job).where(Job.id == job.id))
            stored_run = await session.scalar(
                select(JobRun).where(JobRun.job_id == job.id)
            )
            stored_item = await session.scalar(
                select(JobItem).where(JobItem.job_id == job.id)
            )
            stored_outbox = await session.scalar(
                select(EventOutbox).where(EventOutbox.aggregate_id == str(job.id))
            )

        assert stored is not None
        assert stored.status == JobStatus.PENDING_DISPATCH
        assert stored.created_at.tzinfo is not None
        assert stored_run is not None and stored_run.attempt_no == 1
        assert stored_item is not None and stored_item.item_key == "600000.SH"
        assert stored_outbox is not None
        assert stored_outbox.status == OutboxStatus.PENDING
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_database_rejects_duplicate_scoped_idempotency_key() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    unique = uuid4().hex
    try:
        with pytest.raises(IntegrityError):
            async with database.transaction() as session:
                session.add_all(
                    (
                        new_job(scope=f"test:{unique}", key="same-key"),
                        new_job(scope=f"test:{unique}", key="same-key"),
                    )
                )
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_database_rejects_duplicate_run_attempt_and_item_key() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    unique = uuid4().hex
    try:
        async with database.transaction() as session:
            job = new_job(scope=f"test:{unique}", key=f"key-{unique}")
            session.add(job)
            await session.flush()

        with pytest.raises(IntegrityError):
            async with database.transaction() as session:
                session.add_all(
                    (
                        JobRun(
                            job_id=job.id,
                            attempt_no=1,
                            fence_token=uuid4(),
                            status=JobRunStatus.CLAIMED,
                            soft_timeout_seconds=30,
                            hard_timeout_seconds=60,
                        ),
                        JobRun(
                            job_id=job.id,
                            attempt_no=1,
                            fence_token=uuid4(),
                            status=JobRunStatus.CLAIMED,
                            soft_timeout_seconds=30,
                            hard_timeout_seconds=60,
                        ),
                    )
                )

        with pytest.raises(IntegrityError):
            async with database.transaction() as session:
                session.add_all(
                    (
                        JobItem(
                            job_id=job.id,
                            item_key="600000.SH",
                            status=JobItemStatus.PENDING,
                        ),
                        JobItem(
                            job_id=job.id,
                            item_key="600000.SH",
                            status=JobItemStatus.PENDING,
                        ),
                    )
                )
    finally:
        await database.dispose()
