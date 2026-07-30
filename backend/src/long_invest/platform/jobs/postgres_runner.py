import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import structlog

from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import JobExecutionContext, JobResult
from long_invest.platform.jobs.postgres_service import PostgresJobService

logger = structlog.get_logger(__name__)
PostgresJobHandler = Callable[[JobExecutionContext], Awaitable[JobResult]]


@dataclass(frozen=True, slots=True)
class PostgresRunnerReport:
    recovered: int = 0
    recovery_failed: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0


class PostgresJobRunner:
    def __init__(
        self,
        database: Database,
        handlers: Mapping[str, PostgresJobHandler],
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=60),
        heartbeat_interval: timedelta = timedelta(seconds=20),
        recovery_batch_size: int = 50,
    ) -> None:
        if heartbeat_interval <= timedelta(0):
            raise ValueError("heartbeat interval must be positive")
        if heartbeat_interval >= lease_duration:
            raise ValueError("heartbeat interval must be shorter than the lease")
        self._database = database
        self._handlers = dict(handlers)
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._recovery_batch_size = recovery_batch_size

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        scan_interval: timedelta = timedelta(seconds=1),
    ) -> None:
        if scan_interval <= timedelta(0):
            raise ValueError("scan interval must be positive")
        while not stop.is_set():
            try:
                report = await self.run_once()
                if report.claimed or report.recovered or report.recovery_failed:
                    logger.info(
                        "postgres_job_scan_completed",
                        category="background",
                        claimed=report.claimed,
                        recovered=report.recovered,
                        recovery_failed=report.recovery_failed,
                    )
            except Exception:
                logger.exception(
                    "postgres_job_scan_failed",
                    category="background",
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=scan_interval.total_seconds()
                )

    async def run_once(self) -> PostgresRunnerReport:
        async with self._database.transaction() as session:
            recovered, recovery_failed = await PostgresJobService(
                session
            ).recover_expired(limit=self._recovery_batch_size)

        async with self._database.transaction() as session:
            claim = await PostgresJobService(session).claim_next(
                worker_id=self._worker_id,
                lease_duration=self._lease_duration,
                job_types=tuple(self._handlers),
            )
        if claim is None:
            return PostgresRunnerReport(
                recovered=recovered,
                recovery_failed=recovery_failed,
            )

        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(claim.job_id, claim.lease_token, stop_heartbeat)
        )
        try:
            handler = self._handlers.get(claim.job_type)
            if handler is None:
                result = JobResult.failure(
                    code="JOB_HANDLER_NOT_FOUND",
                    message="没有可执行该任务的处理器",
                    retryable=False,
                )
            else:
                try:
                    async with asyncio.timeout(claim.soft_timeout_seconds):
                        result = await handler(
                            JobExecutionContext(
                                job_id=claim.job_id,
                                fence_token=claim.lease_token,
                                config=claim.config_snapshot,
                                checkpoint=claim.checkpoint,
                            )
                        )
                except TimeoutError:
                    result = JobResult.failure(
                        code="JOB_SOFT_TIMEOUT",
                        message="任务超过软超时限制",
                        retryable=True,
                    )
                except Exception as exc:
                    logger.exception(
                        "postgres_job_handler_failed",
                        category="background",
                        job_id=str(claim.job_id),
                        error_type=type(exc).__name__,
                    )
                    result = JobResult.failure(
                        code="JOB_HANDLER_FAILED",
                        message="任务执行失败",
                        retryable=False,
                    )
        finally:
            stop_heartbeat.set()
            await heartbeat

        async with self._database.transaction() as session:
            service = PostgresJobService(session)
            if result.success:
                accepted = await service.complete(
                    claim.job_id,
                    claim.lease_token,
                    result,
                )
            else:
                accepted = (
                    await service.fail(
                        claim.job_id,
                        claim.lease_token,
                        result,
                        retry_delay=timedelta(seconds=5),
                    )
                    is not None
                )

        return PostgresRunnerReport(
            recovered=recovered,
            recovery_failed=recovery_failed,
            claimed=1,
            succeeded=int(result.success and accepted),
            failed=int(not result.success and accepted),
        )

    async def _heartbeat_loop(
        self,
        job_id: UUID,
        lease_token: UUID,
        stop: asyncio.Event,
    ) -> None:
        interval = self._heartbeat_interval.total_seconds()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                async with self._database.transaction() as session:
                    active = await PostgresJobService(session).renew_lease(
                        job_id,
                        lease_token,
                        lease_duration=self._lease_duration,
                    )
                if not active:
                    return
            except Exception:
                logger.exception(
                    "postgres_job_heartbeat_failed",
                    category="background",
                    job_id=str(job_id),
                )
