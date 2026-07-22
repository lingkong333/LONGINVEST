import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    TERMINAL_JOB_STATUSES,
    JobItemStatus,
    JobProgress,
    JobResult,
    JobRunStatus,
    JobStatus,
    SubmitJob,
)
from long_invest.platform.jobs.models import Job, JobItem, JobRun
from long_invest.platform.jobs.repository import JobRepository
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class JobService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        outbox_writer: TransactionalOutboxWriter | None = None,
    ) -> None:
        self._session = session
        self._jobs = JobRepository(session)
        self._outbox_writer = outbox_writer or TransactionalOutboxWriter()

    async def get(self, job_id: UUID) -> Job | None:
        return await self._jobs.get(job_id)

    async def lock_submission(self, scope: str, key: str) -> None:
        await self._session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"{len(scope)}:{scope}{key}", 0)
                )
            )
        )

    async def find_submission(self, scope: str, key: str) -> Job | None:
        return await self._jobs.find_by_idempotency(scope=scope, key=key)

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
            soft_timeout_seconds=command.soft_timeout_seconds,
            hard_timeout_seconds=command.hard_timeout_seconds,
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
                await self._append_changed(job, change="submitted")
        except IntegrityError:
            existing = await self._jobs.find_by_idempotency(
                scope=command.idempotency_scope,
                key=command.idempotency_key,
            )
            if existing is None:
                raise
            return _resolve_replay(existing, request_hash)
        return job

    async def initialize_items(self, job_id: UUID, item_keys: tuple[str, ...]) -> None:
        if not item_keys or len(item_keys) != len(set(item_keys)):
            raise ValueError("job item keys must be non-empty and unique")
        parent = await self._jobs.lock(job_id)
        if parent is None:
            raise AppError(code="JOB_NOT_FOUND", message="任务不存在", status_code=404)
        existing = await self._jobs.item_keys(job_id)
        if existing and existing != set(item_keys):
            raise AppError(
                code="JOB_ITEM_SCOPE_CONFLICT",
                message="任务项目范围与首次提交不一致",
                status_code=409,
            )
        self._session.add_all(
            JobItem(job_id=job_id, item_key=key, status=JobItemStatus.PENDING)
            for key in item_keys
            if key not in existing
        )
        await self._session.flush()

    async def recover_active_items(self, job_id: UUID, fence_token: UUID) -> bool:
        if await self._active_run(job_id, fence_token) is None:
            return False
        await self._jobs.recover_active_items(job_id)
        return True

    async def control_status(self, job_id: UUID, fence_token: UUID) -> JobStatus | None:
        active = await self._active_run(job_id, fence_token)
        return JobStatus(active[0].status) if active is not None else None

    async def claim_pending_items(
        self, job_id: UUID, fence_token: UUID, *, limit: int
    ) -> tuple[str, ...]:
        if limit <= 0:
            raise ValueError("claim limit must be positive")
        if await self._active_run(job_id, fence_token) is None:
            return ()
        items = await self._jobs.claim_pending_items(job_id, limit=limit)
        now = datetime.now(UTC)
        for item in items:
            item.status = JobItemStatus.FETCHING
            item.attempt_count += 1
            item.started_at = now
            item.ended_at = None
            item.error_code = None
            item.result_ref = None
            item.updated_at = now
        await self._session.flush()
        return tuple(item.item_key for item in items)

    async def set_item_stage(
        self,
        job_id: UUID,
        fence_token: UUID,
        item_key: str,
        status: JobItemStatus,
    ) -> bool:
        if status not in {
            JobItemStatus.FETCHING,
            JobItemStatus.VALIDATING,
            JobItemStatus.RUNNING,
            JobItemStatus.SAVING,
        }:
            raise ValueError("item stage must be active")
        if await self._active_run(job_id, fence_token) is None:
            return False
        item = await self._jobs.lock_item(job_id, item_key)
        if item is None or JobItemStatus(item.status) in {
            JobItemStatus.SUCCEEDED,
            JobItemStatus.SKIPPED,
            JobItemStatus.CANCELED,
        }:
            return False
        item.status = status
        item.updated_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def release_item(
        self, job_id: UUID, fence_token: UUID, item_key: str
    ) -> bool:
        if await self._active_run(job_id, fence_token) is None:
            return False
        item = await self._jobs.lock_item(job_id, item_key)
        if item is None or JobItemStatus(item.status) in {
            JobItemStatus.SUCCEEDED,
            JobItemStatus.SKIPPED,
            JobItemStatus.CANCELED,
        }:
            return False
        item.status = JobItemStatus.PENDING
        item.started_at = None
        item.updated_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def finish_claimed_item(
        self,
        job_id: UUID,
        fence_token: UUID,
        item_key: str,
        *,
        status: JobItemStatus,
        result_ref: dict[str, object] | None = None,
        error_code: str | None = None,
    ) -> bool:
        if status not in {
            JobItemStatus.SUCCEEDED,
            JobItemStatus.FAILED,
            JobItemStatus.SKIPPED,
            JobItemStatus.CANCELED,
        }:
            raise ValueError("item completion status must be terminal")
        if await self._active_run(job_id, fence_token) is None:
            return False
        item = await self._jobs.lock_item(job_id, item_key)
        if item is None or JobItemStatus(item.status) in {
            JobItemStatus.SUCCEEDED,
            JobItemStatus.SKIPPED,
            JobItemStatus.CANCELED,
        }:
            return False
        now = datetime.now(UTC)
        item.status = status
        item.result_ref = result_ref
        item.error_code = error_code
        item.ended_at = now
        item.updated_at = now
        await self._session.flush()
        return True

    async def item_status_counts(
        self, job_id: UUID, fence_token: UUID
    ) -> dict[str, int] | None:
        if await self._active_run(job_id, fence_token) is None:
            return None
        return await self._jobs.item_status_counts(job_id)

    async def cancel_pending_items(self, job_id: UUID, fence_token: UUID) -> bool:
        if await self._active_run(job_id, fence_token) is None:
            return False
        await self._jobs.cancel_pending_items(job_id, datetime.now(UTC))
        return True

    async def request_pause_from_worker(self, job_id: UUID, fence_token: UUID) -> bool:
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, _run = active
        if JobStatus(job.status) is JobStatus.PAUSING:
            return True
        if JobStatus(job.status) is not JobStatus.RUNNING:
            return False
        job.status = JobStatus.PAUSING
        job.updated_at = datetime.now(UTC)
        job.version += 1
        await self._session.flush()
        await self._append_changed(job, change="pause_requested")
        return True

    async def finish_item(
        self,
        *,
        child_job_id: UUID,
        fence_token: UUID,
        parent_job_id: UUID,
        item_key: str,
        status: JobItemStatus,
        result_ref: dict[str, object] | None = None,
        error_code: str | None = None,
    ) -> tuple[int, int, bool]:
        if status not in {
            JobItemStatus.SUCCEEDED,
            JobItemStatus.FAILED,
            JobItemStatus.SKIPPED,
            JobItemStatus.CANCELED,
        }:
            raise ValueError("job item completion requires a terminal status")
        if await self._active_run(child_job_id, fence_token) is None:
            raise AppError(
                code="JOB_FENCE_REJECTED",
                message="任务执行令牌已经失效",
                status_code=409,
            )
        parent = await self._jobs.lock(parent_job_id)
        if parent is None:
            raise AppError(
                code="JOB_NOT_FOUND", message="父任务不存在", status_code=404
            )
        item = await self._jobs.lock_item(parent_job_id, item_key)
        if item is None:
            raise AppError(
                code="JOB_ITEM_NOT_FOUND",
                message="任务项目不存在",
                status_code=404,
            )
        now = datetime.now(UTC)
        changed = False
        if JobItemStatus(item.status) not in {
            JobItemStatus.SUCCEEDED,
            JobItemStatus.SKIPPED,
            JobItemStatus.CANCELED,
        }:
            item.status = status
            item.attempt_count += 1
            item.started_at = item.started_at or now
            item.ended_at = now
            item.result_ref = result_ref
            item.error_code = error_code
            item.updated_at = now
            changed = True
            await self._session.flush()
        completed, total = await self._jobs.item_progress(parent_job_id)
        next_progress = {"completed": completed, "total": total}
        if parent.progress != next_progress:
            parent.progress = next_progress
            parent.updated_at = now
            parent.version += 1
            await self._session.flush()
            await self._append_changed(parent, change="progress")
        return completed, total, changed and total > 0 and completed == total

    async def abandon_item(
        self,
        *,
        parent_job_id: UUID,
        item_key: str,
        error_code: str,
    ) -> tuple[int, int, bool]:
        parent = await self._jobs.lock(parent_job_id)
        if parent is None:
            raise AppError(
                code="JOB_NOT_FOUND", message="父任务不存在", status_code=404
            )
        item = await self._jobs.lock_item(parent_job_id, item_key)
        if item is None:
            raise AppError(
                code="JOB_ITEM_NOT_FOUND",
                message="任务项目不存在",
                status_code=404,
            )
        now = datetime.now(UTC)
        if JobItemStatus(item.status) not in {
            JobItemStatus.SUCCEEDED,
            JobItemStatus.SKIPPED,
            JobItemStatus.CANCELED,
        }:
            item.status = JobItemStatus.FAILED
            item.attempt_count += 1
            item.started_at = item.started_at or now
            item.ended_at = now
            item.error_code = error_code
            item.updated_at = now
            await self._session.flush()
        completed, total = await self._jobs.item_progress(parent_job_id)
        next_progress = {"completed": completed, "total": total}
        if parent.progress != next_progress:
            parent.progress = next_progress
            parent.updated_at = now
            parent.version += 1
            await self._session.flush()
            await self._append_changed(parent, change="progress")
        return completed, total, total > 0 and completed == total

    async def defer(
        self, *, job_id: UUID, fence_token: UUID, result: JobResult
    ) -> bool:
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        now = datetime.now(UTC)
        run.status = JobRunStatus.SUCCEEDED
        run.ended_at = now
        run.heartbeat_at = now
        terminal = JobStatus(job.status) in TERMINAL_JOB_STATUSES
        if not terminal:
            job.status = JobStatus.RUNNING
            job.result_summary = result.as_dict()
        job.current_run_id = None
        job.current_fence_token = None
        job.updated_at = now
        job.version += 1
        await self._session.flush()
        await self._append_changed(job, change="deferred")
        return True

    async def finalize_parent(self, job_id: UUID, result: JobResult) -> bool:
        job = await self._jobs.lock(job_id)
        if job is None:
            return False
        if job.job_type not in {
            "DAILY_DATA_COORDINATE",
            "DAILY_DATA_RETRY",
            "BACKTEST_BULK",
        }:
            raise AppError(
                code="JOB_PARENT_TYPE_INVALID",
                message="任务不是可汇总的日线父任务",
                status_code=409,
            )
        if JobStatus(job.status) in TERMINAL_JOB_STATUSES:
            return True
        now = datetime.now(UTC)
        if result.code == "BACKTEST_BATCH_PAUSED":
            job.status = JobStatus.PAUSED
        elif result.code == "BACKTEST_BATCH_CANCELED":
            job.status = JobStatus.CANCELED
        elif result.success and result.code == "PARTIAL":
            job.status = JobStatus.PARTIAL
        elif result.success:
            job.status = JobStatus.SUCCEEDED
        else:
            job.status = JobStatus.FAILED
        job.result_summary = result.as_dict()
        job.terminal_at = None if job.status is JobStatus.PAUSED else now
        job.updated_at = now
        job.version += 1
        await self._session.flush()
        await self._append_changed(job, change="parent_finalized")
        return True

    async def claim(
        self,
        *,
        job_id: UUID,
        worker_id: str,
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
        if job.current_run_id is not None and job.current_fence_token is not None:
            recovery_run = await self._jobs.lock_run(job.current_run_id)
            if (
                recovery_run is not None
                and recovery_run.fence_token == job.current_fence_token
                and recovery_run.status == JobRunStatus.CLAIMED
                and recovery_run.worker_id is None
            ):
                recovery_run.worker_id = worker_id
                job.status = JobStatus.RUNNING
                job.updated_at = datetime.now(UTC)
                job.version += 1
                await self._session.flush()
                await self._append_changed(job, change="running")
                return recovery_run
            raise AppError(
                code="JOB_RUN_CONFLICT",
                message="任务已有活动执行记录",
                status_code=409,
            )
        if not 0 < job.soft_timeout_seconds <= job.hard_timeout_seconds <= 86400:
            raise AppError(
                code="JOB_TIMEOUT_INVALID",
                message="浠诲姟鍐荤粨瓒呮椂閰嶇疆鏃犳晥",
                status_code=409,
            )

        run = JobRun(
            job_id=job.id,
            attempt_no=await self._jobs.next_attempt_no(job.id),
            worker_id=worker_id,
            fence_token=uuid4(),
            status=JobRunStatus.CLAIMED,
            soft_timeout_seconds=job.soft_timeout_seconds,
            hard_timeout_seconds=job.hard_timeout_seconds,
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
        await self._append_changed(job, change="running")
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
        next_progress = {
            "completed": progress.completed,
            "total": progress.total,
            "message": progress.message,
        }
        if job.progress == next_progress:
            return True
        now = datetime.now(UTC)
        job.progress = next_progress
        job.updated_at = now
        job.version += 1
        run.heartbeat_at = now
        await self._session.flush()
        await self._append_changed(job, change="progress")
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
        job.status = (
            JobStatus.PARTIAL if result.code == "PARTIAL" else JobStatus.SUCCEEDED
        )
        job.result_summary = result.as_dict()
        job.terminal_at = now
        job.updated_at = now
        job.current_run_id = None
        job.current_fence_token = None
        job.version += 1
        await self._session.flush()
        await self._append_changed(job, change="completed")
        return True

    async def pause_at_safe_point(
        self,
        *,
        job_id: UUID,
        fence_token: UUID,
        result: JobResult,
    ) -> bool:
        if not result.success:
            raise ValueError("pause requires a successful JobResult")
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        if JobStatus(job.status) is not JobStatus.PAUSING:
            return False
        now = datetime.now(UTC)
        run.status = JobRunStatus.CANCELED
        run.ended_at = now
        run.heartbeat_at = now
        run.metrics = result.metrics
        job.status = JobStatus.PAUSED
        job.result_summary = result.as_dict()
        job.updated_at = now
        job.current_run_id = None
        job.current_fence_token = None
        job.version += 1
        await self._session.flush()
        await self._append_changed(job, change="paused")
        return True

    async def cancel_at_safe_point(
        self,
        *,
        job_id: UUID,
        fence_token: UUID,
        result: JobResult,
    ) -> bool:
        if not result.success:
            raise ValueError("cancel requires a successful JobResult")
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        if JobStatus(job.status) is not JobStatus.CANCEL_REQUESTED:
            return False
        now = datetime.now(UTC)
        run.status = JobRunStatus.CANCELED
        run.ended_at = now
        run.heartbeat_at = now
        run.metrics = result.metrics
        job.status = JobStatus.CANCELED
        job.result_summary = result.as_dict()
        job.terminal_at = now
        job.updated_at = now
        job.current_run_id = None
        job.current_fence_token = None
        job.version += 1
        await self._session.flush()
        await self._append_changed(job, change="canceled")
        return True

    async def timeout(
        self,
        *,
        job_id: UUID,
        fence_token: UUID,
        result: JobResult,
    ) -> bool:
        if result.success:
            raise ValueError("timeout requires a failed JobResult")
        active = await self._active_run(job_id, fence_token)
        if active is None:
            return False
        job, run = active
        now = datetime.now(UTC)
        run.status = JobRunStatus.TIMED_OUT
        run.ended_at = now
        run.heartbeat_at = now
        run.error_code = result.code
        run.error_summary = result.message[:500]
        run.metrics = result.metrics
        job.status = JobStatus.TIMED_OUT
        job.result_summary = result.as_dict()
        job.terminal_at = now
        job.updated_at = now
        job.current_run_id = None
        job.current_fence_token = None
        job.version += 1
        await self._session.flush()
        await self._append_changed(job, change="timed_out")
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
        await self._append_changed(
            job,
            change="retry_waiting" if result.retryable else "failed",
        )
        return True

    async def append_changed(self, job: Job, *, change: str) -> None:
        await self._append_changed(job, change=change)

    async def _append_changed(self, job: Job, *, change: str) -> None:
        writer = getattr(self, "_outbox_writer", None)
        if writer is None:
            return
        await writer.append(
            session=self._session,
            topic="job.changed.v1",
            aggregate_type="job",
            aggregate_id=str(job.id),
            queue="maintenance",
            payload={
                "job_id": str(job.id),
                "status": JobStatus(job.status).value,
                "version": job.version,
                "progress": job.progress,
                "request_id": job.request_id,
                "change": change,
            },
            dedupe_key=f"job-changed:{job.id}:v{job.version}:{change}",
        )

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
        "soft_timeout_seconds": command.soft_timeout_seconds,
        "hard_timeout_seconds": command.hard_timeout_seconds,
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
