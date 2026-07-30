import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    ClaimedPostgresJob,
    JobProgress,
    JobResult,
    JobStatus,
    SubmitPostgresJob,
)
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.postgres_repository import PostgresJobRepository

PostgresJobAction = Literal["pause", "resume", "cancel", "retry"]


class PostgresJobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jobs = PostgresJobRepository(session)

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
        return await self._jobs.find_by_idempotency(scope, key)

    async def submit(
        self,
        command: SubmitPostgresJob,
        *,
        now: datetime | None = None,
    ) -> Job:
        requested_at = now or datetime.now(UTC)
        request_hash = _request_hash(command)
        existing = await self._jobs.find_by_idempotency(
            command.idempotency_scope, command.idempotency_key
        )
        if existing is not None:
            return _resolve_replay(existing, request_hash)

        job = Job(
            job_type=command.job_type,
            business_object_type=command.business_object_type,
            business_object_id=command.business_object_id,
            queue="postgres",
            module_owner=command.module_owner,
            priority=command.priority,
            status=JobStatus.PENDING,
            config_snapshot=command.config_snapshot,
            idempotency_scope=command.idempotency_scope,
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
            request_id=command.request_id,
            created_by_user_id=command.created_by_user_id,
            soft_timeout_seconds=command.soft_timeout_seconds,
            hard_timeout_seconds=command.hard_timeout_seconds,
            max_attempts=command.max_attempts,
            next_run_at=requested_at,
            recoverable=command.recoverable,
            max_recoveries=command.max_recoveries,
        )
        try:
            async with self._session.begin_nested():
                await self._jobs.add(job)
        except IntegrityError:
            existing = await self._jobs.find_by_idempotency(
                command.idempotency_scope, command.idempotency_key
            )
            if existing is None:
                raise
            return _resolve_replay(existing, request_hash)
        return job

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        job_types: tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> ClaimedPostgresJob | None:
        if not worker_id.strip() or lease_duration <= timedelta(0):
            raise ValueError("worker id and positive lease duration are required")
        claimed_at = now or datetime.now(UTC)
        job = await self._jobs.claim_next(claimed_at, job_types=job_types)
        if job is None:
            return None

        token = uuid4()
        job.status = JobStatus.RUNNING
        job.attempt_count += 1
        job.lease_owner = worker_id.strip()
        job.lease_token = token
        job.lease_expires_at = claimed_at + lease_duration
        job.heartbeat_at = claimed_at
        job.pause_requested = False
        job.cancel_requested = False
        job.updated_at = claimed_at
        job.version += 1
        await self._session.flush()
        return ClaimedPostgresJob(
            job_id=job.id,
            job_type=job.job_type,
            lease_token=token,
            config_snapshot=dict(job.config_snapshot),
            checkpoint=dict(job.checkpoint),
            soft_timeout_seconds=job.soft_timeout_seconds,
            hard_timeout_seconds=job.hard_timeout_seconds,
        )

    async def renew_lease(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> bool:
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        heartbeat_at = now or datetime.now(UTC)
        job = await self._active_job(job_id, lease_token, heartbeat_at)
        if job is None:
            return False
        job.heartbeat_at = heartbeat_at
        job.lease_expires_at = heartbeat_at + lease_duration
        job.updated_at = heartbeat_at
        await self._session.flush()
        return True

    async def report_progress(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        progress: JobProgress,
        checkpoint: dict[str, object],
        lease_duration: timedelta,
        now: datetime | None = None,
        pause: bool = False,
    ) -> JobStatus | None:
        reported_at = now or datetime.now(UTC)
        job = await self._active_job(job_id, lease_token, reported_at)
        if job is None:
            return None
        job.progress = _json_object(
            {
                "completed": progress.completed,
                "total": progress.total,
                "message": progress.message,
            },
            name="job progress",
        )
        job.checkpoint = _json_object(checkpoint, name="job checkpoint")
        job.heartbeat_at = reported_at
        job.lease_expires_at = reported_at + lease_duration
        job.updated_at = reported_at
        job.version += 1
        if pause:
            job.pause_requested = True
        if job.cancel_requested:
            _finish(job, JobStatus.CANCELED, reported_at)
        elif job.pause_requested:
            _finish(job, JobStatus.PAUSED, reported_at)
        await self._session.flush()
        return JobStatus(job.status)

    async def complete(
        self,
        job_id: UUID,
        lease_token: UUID,
        result: JobResult,
        *,
        now: datetime | None = None,
    ) -> bool:
        completed_at = now or datetime.now(UTC)
        job = await self._active_job(job_id, lease_token, completed_at)
        if job is None:
            return False
        job.result_summary = result.as_dict()
        job.last_error_code = None
        job.last_error_summary = None
        if job.cancel_requested:
            status = JobStatus.CANCELED
        elif job.pause_requested:
            status = JobStatus.PAUSED
        elif result.success and result.code == "PARTIAL":
            status = JobStatus.PARTIAL
        elif result.success:
            status = JobStatus.SUCCEEDED
        else:
            raise ValueError("failed job result must use fail")
        _finish(job, status, completed_at)
        await self._session.flush()
        return True

    async def fail(
        self,
        job_id: UUID,
        lease_token: UUID,
        result: JobResult,
        *,
        retry_delay: timedelta = timedelta(0),
        now: datetime | None = None,
    ) -> JobStatus | None:
        if result.success or retry_delay < timedelta(0):
            raise ValueError("failure result and nonnegative retry delay are required")
        failed_at = now or datetime.now(UTC)
        job = await self._active_job(job_id, lease_token, failed_at)
        if job is None:
            return None
        job.result_summary = result.as_dict()
        job.last_error_code = result.code[:100]
        job.last_error_summary = _safe_summary(result.message)
        if job.cancel_requested:
            status = JobStatus.CANCELED
            _finish(job, status, failed_at)
        elif job.pause_requested:
            status = JobStatus.PAUSED
            _finish(job, status, failed_at)
        elif result.retryable and job.attempt_count < job.max_attempts:
            status = JobStatus.PENDING
            job.status = status
            job.next_run_at = failed_at + retry_delay
            _clear_lease(job)
            job.updated_at = failed_at
            job.version += 1
        else:
            status = JobStatus.FAILED
            _finish(job, status, failed_at)
        await self._session.flush()
        return status

    async def command(
        self,
        job_id: UUID,
        action: PostgresJobAction,
        *,
        now: datetime | None = None,
    ) -> Job:
        changed_at = now or datetime.now(UTC)
        job = await self._jobs.lock(job_id)
        if job is None:
            raise _job_error("JOB_NOT_FOUND", "任务不存在", 404)
        status = JobStatus(job.status)
        if action == "pause":
            if status is JobStatus.PENDING:
                job.status = JobStatus.PAUSED
            elif status is JobStatus.RUNNING:
                job.pause_requested = True
            else:
                raise _invalid_action(action, status)
        elif action == "resume":
            if status is not JobStatus.PAUSED:
                raise _invalid_action(action, status)
            job.status = JobStatus.PENDING
            job.next_run_at = changed_at
            job.pause_requested = False
            job.max_attempts = max(job.max_attempts, job.attempt_count + 1)
        elif action == "cancel":
            if status in {JobStatus.PENDING, JobStatus.PAUSED}:
                _finish(job, JobStatus.CANCELED, changed_at)
            elif status is JobStatus.RUNNING:
                job.cancel_requested = True
            else:
                raise _invalid_action(action, status)
        else:
            if status not in {JobStatus.FAILED, JobStatus.PARTIAL}:
                raise _invalid_action(action, status)
            failed_items = _failed_result_items(job)
            if status is JobStatus.PARTIAL and not failed_items:
                raise _invalid_action(action, status)
            if failed_items:
                data = _result_data(job)
                original_total = _nonnegative_int(
                    data.get("total"), default=len(failed_items)
                )
                base_succeeded = _nonnegative_int(data.get("succeeded"))
                counts = {
                    key: _nonnegative_int(data.get(key))
                    for key in (
                        "inserted",
                        "unchanged",
                        "revised",
                        "review_required",
                        "qfq_rows",
                    )
                }
                job.checkpoint = {
                    "retry_items": failed_items,
                    "original_total": original_total,
                    "base_succeeded": base_succeeded,
                    "counts": counts,
                }
                job.progress = {
                    "completed": base_succeeded,
                    "total": original_total,
                    "message": "等待重试失败股票",
                }
            job.status = JobStatus.PENDING
            job.next_run_at = changed_at
            job.max_attempts = max(job.max_attempts, job.attempt_count + 1)
            job.terminal_at = None
        job.updated_at = changed_at
        job.version += 1
        await self._session.flush()
        return job

    async def recover_expired(
        self,
        *,
        limit: int = 50,
        now: datetime | None = None,
    ) -> tuple[int, int]:
        if limit <= 0:
            raise ValueError("recovery limit must be positive")
        recovered_at = now or datetime.now(UTC)
        jobs = await self._jobs.lock_expired(recovered_at, limit=limit)
        recovered = 0
        failed = 0
        for job in jobs:
            job.last_error_code = "JOB_LEASE_EXPIRED"
            job.last_error_summary = "任务执行权过期，原执行结果已失效"
            if job.cancel_requested:
                _finish(job, JobStatus.CANCELED, recovered_at)
            elif job.pause_requested:
                _finish(job, JobStatus.PAUSED, recovered_at)
            elif job.recoverable and job.recovery_count < job.max_recoveries:
                job.recovery_count += 1
                job.status = JobStatus.PENDING
                job.next_run_at = recovered_at
                job.max_attempts = max(job.max_attempts, job.attempt_count + 1)
                _clear_lease(job)
                job.updated_at = recovered_at
                job.version += 1
                recovered += 1
            else:
                _finish(job, JobStatus.FAILED, recovered_at)
                failed += 1
        await self._session.flush()
        return recovered, failed

    async def _active_job(
        self, job_id: UUID, lease_token: UUID, now: datetime
    ) -> Job | None:
        job = await self._jobs.lock(job_id)
        if (
            job is None
            or JobStatus(job.status) is not JobStatus.RUNNING
            or job.lease_token != lease_token
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            return None
        return job


def _failed_result_items(job: Job) -> list[str]:
    data = _result_data(job)
    values = data.get("failed_items")
    if not isinstance(values, list):
        return []
    result = [str(item).strip() for item in values]
    if (
        not result
        or any(not item for item in result)
        or len(result) != len(set(result))
    ):
        return []
    return result


def _result_data(job: Job) -> dict[str, object]:
    summary = job.result_summary
    if not isinstance(summary, dict):
        return {}
    data = summary.get("data")
    return data if isinstance(data, dict) else {}


def _nonnegative_int(value: object, *, default: int = 0) -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _request_hash(command: SubmitPostgresJob) -> str:
    content = {
        "job_type": command.job_type,
        "module_owner": command.module_owner,
        "priority": command.priority,
        "business_object_type": command.business_object_type,
        "business_object_id": command.business_object_id,
        "created_by_user_id": command.created_by_user_id,
        "soft_timeout_seconds": command.soft_timeout_seconds,
        "hard_timeout_seconds": command.hard_timeout_seconds,
        "max_attempts": command.max_attempts,
        "recoverable": command.recoverable,
        "max_recoveries": command.max_recoveries,
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


def _resolve_replay(existing: Job, request_hash: str) -> Job:
    if existing.request_hash != request_hash:
        raise _job_error(
            "IDEMPOTENCY_KEY_REUSED",
            "同一幂等键不能用于不同任务内容",
            409,
        )
    return existing


def _json_object(value: object, *, name: str) -> dict[str, object]:
    try:
        normalized = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-compatible") from exc
    if not isinstance(normalized, dict):
        raise ValueError(f"{name} must be an object")
    return normalized


def _finish(job: Job, status: JobStatus, now: datetime) -> None:
    job.status = status
    job.terminal_at = None if status is JobStatus.PAUSED else now
    job.pause_requested = False
    job.cancel_requested = False
    _clear_lease(job)
    job.updated_at = now
    job.version += 1


def _clear_lease(job: Job) -> None:
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None


def _safe_summary(message: str) -> str:
    summary = " ".join(message.split()).strip()
    return (summary or "任务执行失败")[:500]


def _invalid_action(action: str, status: JobStatus) -> AppError:
    return AppError(
        code="JOB_ACTION_NOT_ALLOWED",
        message="任务当前状态不允许执行该操作",
        status_code=409,
        details={"action": action, "status": status.value},
    )


def _job_error(code: str, message: str, status_code: int) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)
