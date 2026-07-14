import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobProgress,
    JobResult,
    JobRunStatus,
    JobStatus,
    SubmitJob,
)
from long_invest.platform.jobs.models import Job, JobRun
from long_invest.platform.jobs.repository import JobRepository
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


class JobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jobs = JobRepository(session)

    async def submit(self, command: SubmitJob) -> Job:
        request_hash = _request_hash(command)
        existing = await self._jobs.find_by_idempotency(
            scope=command.idempotency_scope,
            key=command.idempotency_key,
        )
        if existing is not None:
            return _resolve_replay(existing, request_hash)

        job_id = uuid4()
        outbox_id = uuid4()
        job = Job(
            id=job_id,
            job_type=command.job_type,
            business_object_type=command.business_object_type,
            business_object_id=command.business_object_id,
            queue=command.queue,
            priority=command.priority,
            status=JobStatus.PENDING_DISPATCH,
            config_snapshot=command.config_snapshot,
            idempotency_scope=command.idempotency_scope,
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
            request_id=command.request_id,
            created_by_user_id=command.created_by_user_id,
        )
        event = EventOutbox(
            id=outbox_id,
            topic="jobs.dispatch",
            aggregate_type="job",
            aggregate_id=str(job_id),
            queue=command.queue,
            payload={
                "job_id": str(job_id),
                "outbox_id": str(outbox_id),
                "job_type": command.job_type,
                "queue": command.queue,
                "request_id": command.request_id,
            },
            dedupe_key=_outbox_dedupe_key(command),
            status=OutboxStatus.PENDING,
        )

        try:
            async with self._session.begin_nested():
                self._session.add_all((job, event))
                await self._session.flush()
        except IntegrityError:
            existing = await self._jobs.find_by_idempotency(
                scope=command.idempotency_scope,
                key=command.idempotency_key,
            )
            if existing is None:
                raise
            return _resolve_replay(existing, request_hash)
        return job

    async def claim(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        soft_timeout_seconds: int,
        hard_timeout_seconds: int,
    ) -> JobRun:
        job = await self._jobs.lock(job_id)
        if job is None:
            raise AppError(
                code="JOB_NOT_FOUND",
                message="任务不存在",
                status_code=404,
            )
        if job.status != JobStatus.QUEUED:
            raise AppError(
                code="JOB_NOT_CLAIMABLE",
                message="任务当前状态不能领取",
                status_code=409,
                details={"status": job.status},
            )
        if hard_timeout_seconds < soft_timeout_seconds:
            raise ValueError("hard timeout cannot be shorter than soft timeout")

        run = JobRun(
            job_id=job.id,
            attempt_no=await self._jobs.next_attempt_no(job.id),
            worker_id=worker_id,
            fence_token=uuid4(),
            status=JobRunStatus.CLAIMED,
            soft_timeout_seconds=soft_timeout_seconds,
            hard_timeout_seconds=hard_timeout_seconds,
        )
        self._session.add(run)
        await self._session.flush()
        now = datetime.now(UTC)
        job.status = JobStatus.RUNNING
        job.current_run_id = run.id
        job.current_fence_token = run.fence_token
        job.updated_at = now
        job.version += 1
        await self._session.flush()
        return run

    async def start(self, *, job_id: UUID, fence_token: UUID) -> bool:
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        now = datetime.now(UTC)
        run.status = JobRunStatus.RUNNING
        run.started_at = run.started_at or now
        run.heartbeat_at = now
        job.updated_at = now
        await self._session.flush()
        return True

    async def heartbeat(self, *, job_id: UUID, fence_token: UUID) -> bool:
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        now = datetime.now(UTC)
        run.heartbeat_at = now
        job.updated_at = now
        await self._session.flush()
        return True

    async def report_progress(
        self,
        *,
        job_id: UUID,
        fence_token: UUID,
        progress: JobProgress,
    ) -> bool:
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        now = datetime.now(UTC)
        job.progress = {
            "completed": progress.completed,
            "total": progress.total,
            "message": progress.message,
        }
        job.updated_at = now
        run.heartbeat_at = now
        await self._session.flush()
        return True

    async def complete(
        self,
        *,
        job_id: UUID,
        fence_token: UUID,
        result: JobResult,
    ) -> bool:
        if not result.success:
            raise ValueError("successful completion requires a successful JobResult")
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        now = datetime.now(UTC)
        run.status = JobRunStatus.SUCCEEDED
        run.ended_at = now
        run.heartbeat_at = now
        run.metrics = result.metrics
        job.status = JobStatus.SUCCEEDED
        job.result_summary = result.as_dict()
        job.terminal_at = now
        job.updated_at = now
        job.current_run_id = None
        job.current_fence_token = None
        job.version += 1
        await self._session.flush()
        return True

    async def fail(
        self,
        *,
        job_id: UUID,
        fence_token: UUID,
        result: JobResult,
    ) -> bool:
        if result.success:
            raise ValueError("failure requires a failed JobResult")
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        now = datetime.now(UTC)
        run.status = JobRunStatus.FAILED
        run.ended_at = now
        run.heartbeat_at = now
        run.error_code = result.code
        run.error_summary = result.message[:500]
        run.metrics = result.metrics
        job.status = JobStatus.WAITING_RETRY if result.retryable else JobStatus.FAILED
        job.result_summary = result.as_dict()
        job.terminal_at = None if result.retryable else now
        job.updated_at = now
        job.current_run_id = None
        job.current_fence_token = None
        job.version += 1
        await self._session.flush()
        return True

    async def _active_run(
        self,
        job_id: UUID,
        fence_token: UUID,
    ) -> tuple[Job, JobRun] | None:
        job = await self._jobs.lock(job_id)
        if job is None:
            return None
        if job.current_fence_token != fence_token or job.current_run_id is None:
            stale = await self._jobs.lock_run_by_fence(fence_token)
            if stale is not None and stale.status in {
                JobRunStatus.CLAIMED,
                JobRunStatus.STARTING,
                JobRunStatus.RUNNING,
                JobRunStatus.LOST,
            }:
                stale.status = JobRunStatus.SUPERSEDED
                stale.ended_at = stale.ended_at or datetime.now(UTC)
                await self._session.flush()
            return None
        run = await self._jobs.lock_run(job.current_run_id)
        if run is None or run.fence_token != fence_token:
            return None
        if run.status not in {
            JobRunStatus.CLAIMED,
            JobRunStatus.STARTING,
            JobRunStatus.RUNNING,
        }:
            return None
        return job, run


def _request_hash(command: SubmitJob) -> str:
    content = {
        "job_type": command.job_type,
        "queue": command.queue,
        "priority": command.priority,
        "business_object_type": command.business_object_type,
        "business_object_id": command.business_object_id,
        "created_by_user_id": command.created_by_user_id,
        "config_snapshot": command.config_snapshot,
    }
    serialized = json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _outbox_dedupe_key(command: SubmitJob) -> str:
    raw = f"{command.idempotency_scope}\0{command.idempotency_key}".encode()
    return f"job:{hashlib.sha256(raw).hexdigest()}"


def _resolve_replay(existing: Job, request_hash: str) -> Job:
    if existing.request_hash != request_hash:
        raise AppError(
            code="IDEMPOTENCY_KEY_REUSED",
            message="同一幂等键不能用于不同任务内容",
            status_code=409,
            details={"existing_job_id": str(existing.id)},
        )
    return existing
