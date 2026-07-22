import asyncio
from uuid import uuid4

import pytest
from redis import Redis
from rq import Queue
from sqlalchemy import delete, select

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import JobRunStatus, JobStatus, SubmitJob
from long_invest.platform.jobs.models import Job, JobRun
from long_invest.platform.jobs.service import JobService
from long_invest.platform.jobs.worker import execute_job
from long_invest.platform.outbox.dispatcher import OutboxDispatcher
from long_invest.platform.outbox.models import EventOutbox
from long_invest.platform.queue.rq import RqQueuePublisher


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_system_noop_flows_through_database_rq_and_fenced_worker() -> None:
    settings = AppSettings(_env_file=None)
    database = Database(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    queue = Queue("maintenance", connection=redis)
    publisher = RqQueuePublisher(settings.redis_url)
    unique = uuid4().hex
    job_id = None
    try:
        await asyncio.to_thread(queue.empty)
        async with database.transaction() as session:
            job = await JobService(session).submit(
                SubmitJob(
                    job_type="SYSTEM_NOOP",
                    queue="maintenance",
                    idempotency_scope=f"integration:{unique}",
                    idempotency_key=f"key-{unique}",
                    request_id=f"req_{unique}",
                    config_snapshot={"check": "jobs-outbox"},
                )
            )
            job_id = job.id
        report = await OutboxDispatcher(
            database=database,
            publisher=publisher,
            dispatcher_id="integration-dispatcher",
        ).dispatch_once()

        async with database.session() as session:
            outbox = await session.scalar(
                select(EventOutbox).where(
                    EventOutbox.aggregate_id == str(job.id),
                    EventOutbox.topic == "jobs.dispatch",
                )
            )
        assert outbox is not None
        queued_ids = await asyncio.to_thread(queue.get_job_ids)
        assert outbox.rq_job_id in queued_ids

        result = await asyncio.to_thread(execute_job, str(job.id), str(outbox.id))

        async with database.session() as session:
            stored_job = await session.get(Job, job.id)
            run = await session.scalar(select(JobRun).where(JobRun.job_id == job.id))
        assert report.dispatched >= 1
        assert result["success"] is True
        assert stored_job is not None and stored_job.status == JobStatus.SUCCEEDED
        assert stored_job.result_summary is not None
        assert stored_job.result_summary["data"] == {"check": "jobs-outbox"}
        assert run is not None and run.status == JobRunStatus.SUCCEEDED
    finally:
        await asyncio.to_thread(queue.empty)
        await publisher.close()
        await asyncio.to_thread(redis.close)
        if job_id is not None:
            async with database.transaction() as session:
                await session.execute(
                    delete(EventOutbox).where(
                        EventOutbox.aggregate_id == str(job_id)
                    )
                )
                await session.execute(delete(Job).where(Job.id == job_id))
        await database.dispose()
