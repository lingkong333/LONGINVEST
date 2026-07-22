import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import structlog

from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobItemStatus,
    JobResult,
    linked_job_item,
    linked_parent_job_id,
)
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.service import JobService
from long_invest.platform.logging.configure import configure_logging

logger = structlog.get_logger(__name__)
JobHandler = Callable[[JobExecutionContext], Awaitable[JobResult]]


async def _system_noop(context: JobExecutionContext) -> JobResult:
    return JobResult.success_result(data=dict(context.config))


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
            )
            await service.start(job_id=job.id, fence_token=run.fence_token)
            config_snapshot = dict(job.config_snapshot)

        timed_out = False
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                database,
                job_id,
                run.fence_token,
                heartbeat_stop,
                interval_seconds=float(
                    getattr(settings, "job_heartbeat_interval_seconds", 15)
                ),
            )
        )
        if handler is None:
            result = JobResult.failure(
                code="JOB_HANDLER_NOT_FOUND",
                message="没有可执行该任务的处理器",
                retryable=False,
            )
        else:
            try:
                async with asyncio.timeout(job.soft_timeout_seconds):
                    result = await handler(
                        JobExecutionContext(
                            job_id=job_id,
                            fence_token=run.fence_token,
                            config=config_snapshot,
                        )
                    )
            except TimeoutError:
                timed_out = True
                result = JobResult.failure(
                    code="JOB_SOFT_TIMEOUT",
                    message="浠诲姟瓒呭嚭杞秴鏃堕檺鍒?",
                    retryable=False,
                )
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

        heartbeat_stop.set()
        await heartbeat_task

        async with database.transaction() as session:
            service = JobService(session)
            linked = linked_job_item(config_snapshot)
            if not result.success and linked is not None:
                _completed, _total, all_terminal = await service.finish_item(
                    child_job_id=job_id,
                    fence_token=run.fence_token,
                    parent_job_id=linked.parent_job_id,
                    item_key=linked.item_key,
                    status=JobItemStatus.FAILED,
                    error_code=result.code,
                )
                if all_terminal:
                    await service.submit(linked.completion_job)
            parent_job_id = linked_parent_job_id(config_snapshot)
            if timed_out:
                accepted = await service.timeout(
                    job_id=job_id,
                    fence_token=run.fence_token,
                    result=result,
                )
            elif result.success and result.code == "CHILDREN_PENDING":
                accepted = await service.defer(
                    job_id=job_id,
                    fence_token=run.fence_token,
                    result=result,
                )
            elif result.success and result.code == "HISTORY_BACKFILL_PAUSED":
                accepted = await service.pause_at_safe_point(
                    job_id=job_id,
                    fence_token=run.fence_token,
                    result=result,
                )
            elif result.success and result.code == "HISTORY_BACKFILL_CANCELED":
                accepted = await service.cancel_at_safe_point(
                    job_id=job_id,
                    fence_token=run.fence_token,
                    result=result,
                )
            elif result.success:
                accepted = await service.complete(
                    job_id=job_id,
                    fence_token=run.fence_token,
                    result=result,
                )
            else:
                accepted = await service.fail(
                    job_id=job_id,
                    fence_token=run.fence_token,
                    result=result,
                )
            if not result.success and parent_job_id is not None and accepted:
                await service.finalize_parent(parent_job_id, result)
        return result
    finally:
        await database.dispose()


async def _heartbeat_loop(
    database: Database,
    job_id: UUID,
    fence_token: UUID,
    stop: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            async with database.transaction() as session:
                active = await JobService(session).heartbeat(
                    job_id=job_id, fence_token=fence_token
                )
            if not active:
                return
        except Exception:
            logger.exception(
                "job_heartbeat_failed",
                category="worker",
                job_id=str(job_id),
            )
            continue
