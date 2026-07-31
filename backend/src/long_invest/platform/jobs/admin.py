from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobItemStatus,
    JobStatus,
)
from long_invest.platform.jobs.models import Job, JobItem, JobRun
from long_invest.platform.jobs.postgres_service import PostgresJobService

JobAction = Literal["cancel", "pause", "resume", "retry", "retry-failed-items"]

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
        module_owner: str | None = None,
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
        if module_owner is not None:
            filters.append(Job.module_owner == module_owner)
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
        if _is_postgres_job(job):
            return _postgres_allowed_actions(job)
        return ()

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

        if not _is_postgres_job(job):
            raise AppError(
                code="LEGACY_JOB_READ_ONLY",
                message="旧版任务仅供历史查询，不能再执行控制操作",
                status_code=409,
            )
        return await self._postgres_command(job, action, context, audit_key)

    async def _postgres_command(
        self,
        job: Job,
        action: JobAction,
        context: JobCommandContext,
        audit_key: str,
    ) -> Job:
        if action == "retry-failed-items":
            raise _action_not_allowed(action, JobStatus(job.status))
        before = _state_summary(job)
        changed = await PostgresJobService(self._session).command(job.id, action)
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
                after_summary=_state_summary(changed),
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )
        await self._session.flush()
        return changed

    async def _lock_idempotency(self, key: str) -> None:
        await self._session.scalar(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )


def _is_postgres_job(job: Job) -> bool:
    return job.queue == "postgres" and job.module_owner != "legacy"


def _postgres_allowed_actions(job: Job) -> tuple[str, ...]:
    status = JobStatus(job.status)
    if status is JobStatus.PENDING:
        return ("cancel", "pause")
    if status is JobStatus.RUNNING:
        return ("cancel", "pause")
    if status is JobStatus.PAUSED:
        return ("cancel", "resume")
    if status is JobStatus.FAILED:
        return ("retry",)
    if status is JobStatus.PARTIAL and _postgres_failed_result_items(job):
        return ("retry",)
    return ()


def _postgres_failed_result_items(job: Job) -> tuple[str, ...]:
    summary = job.result_summary
    if not isinstance(summary, dict):
        return ()
    data = summary.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("failed_items"), list):
        return ()
    return tuple(str(item) for item in data["failed_items"] if str(item).strip())


def _state_summary(job: Job) -> dict[str, Any]:
    return {
        "status": str(job.status),
        "version": job.version,
        "current_run_id": str(job.current_run_id) if job.current_run_id else None,
    }


def _audit_key(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return f"job-admin:{digest}"


def _not_found() -> AppError:
    return AppError(code="JOB_NOT_FOUND", message="任务不存在", status_code=404)


def _action_not_allowed(action: str, status: JobStatus) -> AppError:
    return AppError(
        code="JOB_ACTION_NOT_ALLOWED",
        message="任务当前状态不允许执行该操作",
        status_code=409,
        details={"action": action, "status": status.value},
    )
