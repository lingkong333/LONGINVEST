from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobItemStatus,
    JobRunStatus,
    JobStatus,
)
from long_invest.platform.jobs.models import Job, JobItem, JobRun
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus

JobAction = Literal["cancel", "pause", "resume", "retry", "retry-failed-items"]

_CANCEL_IMMEDIATE = {
    JobStatus.PENDING_DISPATCH,
    JobStatus.QUEUED,
    JobStatus.WAITING_RETRY,
    JobStatus.PAUSED,
    JobStatus.BLOCKED,
}
_CANCEL_REQUEST = {JobStatus.RUNNING, JobStatus.PAUSING}
_RETRYABLE = {JobStatus.FAILED, JobStatus.TIMED_OUT, JobStatus.LOST}
_PAUSABLE_QUEUES = {"bulk-backtest", "bulk-history", "exports"}


@dataclass(frozen=True, slots=True)
class JobCommandContext:
    request_id: str
    idempotency_key: str
    actor_user_id: str
    reason: str
    expected_version: int
    session_id: str | None = None
    trusted_ip: str | None = None


@dataclass(frozen=True, slots=True)
class JobPage:
    items: tuple[Job, ...]
    page: int
    page_size: int
    total: int


class JobAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list_jobs(
        self,
        *,
        page: int,
        page_size: int,
        status: JobStatus | None = None,
        job_type: str | None = None,
        queue: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> JobPage:
        filters = []
        if status is not None:
            filters.append(Job.status == status)
        if job_type is not None:
            filters.append(Job.job_type == job_type)
        if queue is not None:
            filters.append(Job.queue == queue)
        if created_from is not None:
            filters.append(Job.created_at >= created_from)
        if created_to is not None:
            filters.append(Job.created_at <= created_to)
        total = await self._session.scalar(
            select(func.count()).select_from(Job).where(*filters)
        )
        rows = await self._session.scalars(
            select(Job)
            .where(*filters)
            .order_by(Job.created_at.desc(), Job.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return JobPage(tuple(rows.all()), page, page_size, int(total or 0))

    async def get_job(self, job_id: UUID) -> Job:
        job = await self._session.get(Job, job_id)
        if job is None:
            raise _not_found()
        return job

    async def list_runs(self, job_id: UUID) -> tuple[JobRun, ...]:
        await self.get_job(job_id)
        rows = await self._session.scalars(
            select(JobRun)
            .where(JobRun.job_id == job_id)
            .order_by(JobRun.attempt_no.desc())
        )
        return tuple(rows.all())

    async def list_items(
        self,
        job_id: UUID,
        *,
        page: int,
        page_size: int,
        status: JobItemStatus | None = None,
    ) -> tuple[tuple[JobItem, ...], int]:
        await self.get_job(job_id)
        filters = [JobItem.job_id == job_id]
        if status is not None:
            filters.append(JobItem.status == status)
        total = await self._session.scalar(
            select(func.count()).select_from(JobItem).where(*filters)
        )
        rows = await self._session.scalars(
            select(JobItem)
            .where(*filters)
            .order_by(JobItem.item_key, JobItem.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return tuple(rows.all()), int(total or 0)

    async def allowed_actions(self, job_id: UUID) -> tuple[str, ...]:
        job = await self.get_job(job_id)
        failed_items = await self._failed_item_count(job_id)
        return _allowed_actions(job, failed_items > 0)

    async def command(
        self,
        job_id: UUID,
        action: JobAction,
        context: JobCommandContext,
    ) -> Job:
        audit_key = _audit_key(context.idempotency_key)
        await self._lock_idempotency(audit_key)
        replay = await self._audit.find_by_idempotency(audit_key)
        if replay is not None:
            if replay.action_code != f"JOB_{action.upper().replace('-', '_')}" or (
                replay.object_id != str(job_id)
            ):
                raise AppError(
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="同一幂等键不能用于不同任务操作",
                    status_code=409,
                )
            return await self.get_job(job_id)

        job = await self._session.scalar(
            select(Job).where(Job.id == job_id).with_for_update()
        )
        if job is None:
            raise _not_found()
        if job.version != context.expected_version:
            raise AppError(
                code="JOB_VERSION_CONFLICT",
                message="任务状态已经变化，请刷新后重试",
                status_code=409,
                details={"current_version": job.version},
            )

        before = _state_summary(job)
        now = datetime.now(UTC)
        if action == "cancel":
            await self._cancel(job, now)
        elif action == "pause":
            self._pause(job, now)
        elif action in {"resume", "retry"}:
            await self._redispatch(job, action, now)
        else:
            await self._retry_failed_items(job, now)

        job.version += 1
        job.updated_at = now
        await self._session.flush()
        if action in {"cancel", "pause"}:
            self._session.add(_control_event(job, action, context.request_id))
        await self._audit.append(
            AuditWrite(
                action_code=f"JOB_{action.upper().replace('-', '_')}",
                object_type="job",
                object_id=str(job.id),
                result="SUCCESS",
                request_id=context.request_id,
                idempotency_key=audit_key,
                risk_level="HIGH",
                reason=context.reason,
                before_summary=before,
                after_summary=_state_summary(job),
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )
        await self._session.flush()
        return job

    async def _cancel(self, job: Job, now: datetime) -> None:
        status = JobStatus(job.status)
        if status in _CANCEL_IMMEDIATE:
            if job.current_run_id is not None:
                run = await self._session.scalar(
                    select(JobRun)
                    .where(JobRun.id == job.current_run_id)
                    .with_for_update()
                )
                if run is not None and JobRunStatus(run.status) in {
                    JobRunStatus.CLAIMED,
                    JobRunStatus.STARTING,
                    JobRunStatus.RUNNING,
                }:
                    run.status = JobRunStatus.CANCELED
                    run.ended_at = now
            job.status = JobStatus.CANCELED
            job.terminal_at = now
            job.current_run_id = None
            job.current_fence_token = None
            return
        if status in _CANCEL_REQUEST:
            job.status = JobStatus.CANCEL_REQUESTED
            return
        raise _action_not_allowed("cancel", status)

    def _pause(self, job: Job, _now: datetime) -> None:
        status = JobStatus(job.status)
        if status != JobStatus.RUNNING or job.queue not in _PAUSABLE_QUEUES:
            raise _action_not_allowed("pause", status)
        job.status = JobStatus.PAUSING

    async def _redispatch(
        self, job: Job, action: Literal["resume", "retry"], now: datetime
    ) -> None:
        status = JobStatus(job.status)
        valid = (
            status == JobStatus.PAUSED if action == "resume" else status in _RETRYABLE
        )
        if not valid:
            raise _action_not_allowed(action, status)
        await self._new_run_and_dispatch(job, action, now)

    async def _retry_failed_items(self, job: Job, now: datetime) -> None:
        failed = (
            await self._session.scalars(
                select(JobItem)
                .where(
                    JobItem.job_id == job.id,
                    JobItem.status == JobItemStatus.FAILED,
                )
                .order_by(JobItem.item_key)
                .with_for_update()
            )
        ).all()
        if not failed:
            raise AppError(
                code="JOB_NO_FAILED_ITEMS",
                message="任务没有可重试的失败项目",
                status_code=409,
            )
        if JobStatus(job.status) not in {
            JobStatus.PARTIAL,
            JobStatus.FAILED,
            JobStatus.TIMED_OUT,
            JobStatus.LOST,
        }:
            raise _action_not_allowed("retry-failed-items", JobStatus(job.status))
        for item in failed:
            item.status = JobItemStatus.PENDING
            item.error_code = None
            item.started_at = None
            item.ended_at = None
            item.updated_at = now
        completed, total = await self._item_progress(job.id)
        job.progress = {"completed": completed, "total": total}
        await self._new_run_and_dispatch(job, "retry-failed-items", now)

    async def _new_run_and_dispatch(
        self, job: Job, action: JobAction, now: datetime
    ) -> None:
        if job.current_run_id is not None:
            previous = await self._session.scalar(
                select(JobRun).where(JobRun.id == job.current_run_id).with_for_update()
            )
            if previous is not None and JobRunStatus(previous.status) in {
                JobRunStatus.CLAIMED,
                JobRunStatus.STARTING,
                JobRunStatus.RUNNING,
            }:
                previous.status = (
                    JobRunStatus.CANCELED
                    if action == "resume"
                    else JobRunStatus.SUPERSEDED
                )
                previous.exit_type = action.upper().replace("-", "_")
                previous.ended_at = now
        attempt_no = await self._session.scalar(
            select(func.max(JobRun.attempt_no)).where(JobRun.job_id == job.id)
        )
        run = JobRun(
            job_id=job.id,
            attempt_no=int(attempt_no or 0) + 1,
            worker_id=None,
            fence_token=uuid4(),
            status=JobRunStatus.CLAIMED,
            soft_timeout_seconds=job.soft_timeout_seconds,
            hard_timeout_seconds=job.hard_timeout_seconds,
            claimed_at=now,
        )
        self._session.add(run)
        await self._session.flush()
        event = _dispatch_event(job, run, action)
        self._session.add(event)
        job.status = JobStatus.PENDING_DISPATCH
        job.current_run_id = run.id
        job.current_fence_token = run.fence_token
        job.terminal_at = None

    async def _failed_item_count(self, job_id: UUID) -> int:
        value = await self._session.scalar(
            select(func.count())
            .select_from(JobItem)
            .where(
                JobItem.job_id == job_id,
                JobItem.status == JobItemStatus.FAILED,
            )
        )
        return int(value or 0)

    async def _item_progress(self, job_id: UUID) -> tuple[int, int]:
        total = await self._session.scalar(
            select(func.count()).select_from(JobItem).where(JobItem.job_id == job_id)
        )
        completed = await self._session.scalar(
            select(func.count())
            .select_from(JobItem)
            .where(
                JobItem.job_id == job_id,
                JobItem.status.in_(
                    (
                        JobItemStatus.SUCCEEDED,
                        JobItemStatus.FAILED,
                        JobItemStatus.SKIPPED,
                        JobItemStatus.CANCELED,
                    )
                ),
            )
        )
        return int(completed or 0), int(total or 0)

    async def _lock_idempotency(self, key: str) -> None:
        await self._session.scalar(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )


def _allowed_actions(job: Job, has_failed_items: bool) -> tuple[str, ...]:
    status = JobStatus(job.status)
    actions: list[str] = []
    if status in _CANCEL_IMMEDIATE | _CANCEL_REQUEST:
        actions.append("cancel")
    if status == JobStatus.RUNNING and job.queue in _PAUSABLE_QUEUES:
        actions.append("pause")
    if status == JobStatus.PAUSED:
        actions.append("resume")
    if status in _RETRYABLE:
        actions.append("retry")
    if has_failed_items and status in {
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.TIMED_OUT,
        JobStatus.LOST,
    }:
        actions.append("retry-failed-items")
    return tuple(actions)


def _state_summary(job: Job) -> dict[str, Any]:
    return {
        "status": str(job.status),
        "version": job.version,
        "current_run_id": str(job.current_run_id) if job.current_run_id else None,
    }


def _audit_key(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return f"job-admin:{digest}"


def _dispatch_event(job: Job, run: JobRun, action: JobAction) -> EventOutbox:
    event_id = uuid4()
    return EventOutbox(
        id=event_id,
        topic="jobs.dispatch",
        aggregate_type="job",
        aggregate_id=str(job.id),
        queue=job.queue,
        payload={
            "job_id": str(job.id),
            "outbox_id": str(event_id),
            "job_type": job.job_type,
            "queue": job.queue,
            "request_id": job.request_id,
            "run_id": str(run.id),
            "action": action,
        },
        dedupe_key=f"job:{job.id}:run:{run.attempt_no}",
        status=OutboxStatus.PENDING,
    )


def _control_event(job: Job, action: JobAction, request_id: str) -> EventOutbox:
    event_id = uuid4()
    return EventOutbox(
        id=event_id,
        topic="jobs.control",
        aggregate_type="job",
        aggregate_id=str(job.id),
        queue=job.queue,
        payload={
            "job_id": str(job.id),
            "outbox_id": str(event_id),
            "action": action,
            "request_id": request_id,
            "version": job.version,
        },
        dedupe_key=f"job:{job.id}:control:{action}:v{job.version}",
        status=OutboxStatus.PENDING,
    )


def _not_found() -> AppError:
    return AppError(code="JOB_NOT_FOUND", message="任务不存在", status_code=404)


def _action_not_allowed(action: str, status: JobStatus) -> AppError:
    return AppError(
        code="JOB_ACTION_NOT_ALLOWED",
        message="任务当前状态不允许执行该操作",
        status_code=409,
        details={"action": action, "status": status.value},
    )
