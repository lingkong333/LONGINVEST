import asyncio
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import JobStatus, SubmitJob
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.service import JobService
from long_invest.platform.outbox.dispatcher import OutboxDispatcher
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class FakePublisher:
    fail: bool = False
    delay: float = 0
    published_ids: list[str] = field(default_factory=list)
    created_ids: set[str] = field(default_factory=set)

    async def publish(
        self,
        *,
        queue: str,
        outbox_id: UUID,
        job_id: UUID,
        timeout_seconds: int,
    ) -> str:
        del queue, job_id, timeout_seconds
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("redis unavailable")
        rq_job_id = f"outbox-{outbox_id}"
        self.published_ids.append(rq_job_id)
        self.created_ids.add(rq_job_id)
        return rq_job_id


async def submit_test_job(database: Database) -> Job:
    unique = uuid4().hex
    async with database.transaction() as session:
        return await JobService(session).submit(
            SubmitJob(
                job_type="FOUNDATION_TEST",
                queue="maintenance",
                idempotency_scope=f"dispatcher:{unique}",
                idempotency_key=f"key-{unique}",
                request_id=f"req_{unique}",
                config_snapshot={"sample": True},
            )
        )


async def clean_foundation_jobs(database: Database) -> None:
    async with database.transaction() as session:
        job_ids = (
            await session.scalars(
                select(Job.id).where(Job.job_type == "FOUNDATION_TEST")
            )
        ).all()
        if not job_ids:
            return
        aggregate_ids = [str(job_id) for job_id in job_ids]
        await session.execute(
            delete(EventOutbox).where(EventOutbox.aggregate_id.in_(aggregate_ids))
        )
        await session.execute(delete(Job).where(Job.id.in_(job_ids)))


@pytest.mark.anyio
async def test_successful_dispatch_marks_outbox_and_job_queued() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    publisher = FakePublisher()
    try:
        await clean_foundation_jobs(database)
        job = await submit_test_job(database)
        report = await OutboxDispatcher(
            database=database,
            publisher=publisher,
            dispatcher_id="dispatcher-a",
        ).dispatch_once()

        async with database.session() as session:
            stored_job = await session.get(Job, job.id)
            outbox = await session.scalar(
                select(EventOutbox).where(EventOutbox.aggregate_id == str(job.id))
            )

        assert report.claimed == 1
        assert report.dispatched == 1
        assert report.failed == 0
        assert stored_job is not None and stored_job.status == JobStatus.QUEUED
        assert outbox is not None and outbox.status == OutboxStatus.DISPATCHED
        assert outbox.rq_job_id == f"outbox-{outbox.id}"
    finally:
        await clean_foundation_jobs(database)
        await database.dispose()


@pytest.mark.anyio
async def test_redis_failure_returns_outbox_to_pending() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        await clean_foundation_jobs(database)
        job = await submit_test_job(database)
        report = await OutboxDispatcher(
            database=database,
            publisher=FakePublisher(fail=True),
            dispatcher_id="dispatcher-a",
        ).dispatch_once()

        async with database.session() as session:
            stored_job = await session.get(Job, job.id)
            outbox = await session.scalar(
                select(EventOutbox).where(EventOutbox.aggregate_id == str(job.id))
            )

        assert report.failed == 1
        assert stored_job is not None
        assert stored_job.status == JobStatus.PENDING_DISPATCH
        assert outbox is not None and outbox.status == OutboxStatus.PENDING
        assert outbox.attempt_count == 1
        assert outbox.last_error_code == "QUEUE_UNAVAILABLE"
    finally:
        await clean_foundation_jobs(database)
        await database.dispose()


@pytest.mark.anyio
async def test_concurrent_dispatchers_claim_one_outbox_once() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    publisher = FakePublisher(delay=0.05)
    try:
        await clean_foundation_jobs(database)
        await submit_test_job(database)
        reports = await asyncio.gather(
            OutboxDispatcher(
                database=database,
                publisher=publisher,
                dispatcher_id="dispatcher-a",
            ).dispatch_once(),
            OutboxDispatcher(
                database=database,
                publisher=publisher,
                dispatcher_id="dispatcher-b",
            ).dispatch_once(),
        )

        assert sum(report.claimed for report in reports) == 1
        assert len(publisher.published_ids) == 1
    finally:
        await clean_foundation_jobs(database)
        await database.dispose()


@pytest.mark.anyio
async def test_replayed_dispatch_uses_same_deterministic_rq_id() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    publisher = FakePublisher()
    dispatcher = OutboxDispatcher(
        database=database,
        publisher=publisher,
        dispatcher_id="dispatcher-a",
    )
    try:
        await clean_foundation_jobs(database)
        job = await submit_test_job(database)
        await dispatcher.dispatch_once()
        async with database.transaction() as session:
            outbox = await session.scalar(
                select(EventOutbox).where(EventOutbox.aggregate_id == str(job.id))
            )
            assert outbox is not None
            outbox.status = OutboxStatus.PENDING
            outbox.dispatched_at = None
            outbox.rq_job_id = None
            stored_job = await session.get(Job, job.id)
            assert stored_job is not None
            stored_job.status = JobStatus.PENDING_DISPATCH

        await dispatcher.dispatch_once()

        assert len(publisher.published_ids) == 2
        assert len(publisher.created_ids) == 1
    finally:
        await clean_foundation_jobs(database)
        await database.dispose()
