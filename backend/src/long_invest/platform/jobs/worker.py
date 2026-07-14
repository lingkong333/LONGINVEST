import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import structlog

from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import JobResult
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.service import JobService
from long_invest.platform.logging.configure import configure_logging

logger = structlog.get_logger(__name__)
JobHandler = Callable[[dict[str, Any]], Awaitable[JobResult]]


async def _system_noop(config_snapshot: dict[str, Any]) -> JobResult:
    return JobResult.success_result(data=config_snapshot)


HANDLERS: dict[str, JobHandler] = {
    "SYSTEM_NOOP": _system_noop,
}


def execute_job(job_id: str, outbox_id: str) -> dict[str, Any]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service="longinvest-worker",
    )
    return asyncio.run(_execute_job(UUID(job_id), UUID(outbox_id))).as_dict()


async def _execute_job(job_id: UUID, outbox_id: UUID) -> JobResult:
    settings = get_settings()
    database = Database(settings.database_url)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    try:
        async with database.transaction() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return JobResult.failure(
                    code="JOB_NOT_FOUND",
                    message="任务不存在",
                    retryable=False,
                )
            handler = HANDLERS.get(job.job_type)
            service = JobService(session)
            run = await service.claim(
                job_id=job.id,
                worker_id=worker_id,
                soft_timeout_seconds=30,
                hard_timeout_seconds=settings.queue_job_timeout_seconds,
            )
            await service.start(job_id=job.id, fence_token=run.fence_token)
            config_snapshot = dict(job.config_snapshot)

        if handler is None:
            result = JobResult.failure(
                code="JOB_HANDLER_NOT_FOUND",
                message="没有可执行该任务的处理器",
                retryable=False,
            )
        else:
            try:
                result = await handler(config_snapshot)
            except Exception as exc:
                logger.exception(
                    "job_handler_failed",
                    category="worker",
                    job_id=str(job_id),
                    outbox_id=str(outbox_id),
                    error_type=type(exc).__name__,
                )
                result = JobResult.failure(
                    code="JOB_HANDLER_FAILED",
                    message="任务执行失败",
                    retryable=False,
                )

        async with database.transaction() as session:
            service = JobService(session)
            if result.success:
                await service.complete(
                    job_id=job_id,
                    fence_token=run.fence_token,
                    result=result,
                )
            else:
                await service.fail(
                    job_id=job_id,
                    fence_token=run.fence_token,
                    result=result,
                )
        return result
    finally:
        await database.dispose()
