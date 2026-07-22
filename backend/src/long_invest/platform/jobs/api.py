from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope
from long_invest.platform.jobs.admin import JobAction, JobCommandContext
from long_invest.platform.jobs.application import JobAdminApplication
from long_invest.platform.jobs.contracts import JobItemStatus, JobStatus

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])
_application_factory: Callable[[], JobAdminApplication] | None = None


def configure_job_admin_application(
    factory: Callable[[], JobAdminApplication],
) -> None:
    global _application_factory
    _application_factory = factory


def get_job_admin_application() -> JobAdminApplication:
    if _application_factory is None:
        raise AppError(
            code="JOB_ADMIN_NOT_CONFIGURED",
            message="任务中心尚未完成生产装配",
            status_code=503,
        )
    return _application_factory()


Application = Annotated[JobAdminApplication, Depends(get_job_admin_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]


class JobCommandBody(BaseModel):
    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class JobPageData(BaseModel):
    items: list[dict[str, Any]]
    pagination: Pagination


class JobPageResponse(SuccessEnvelope):
    data: JobPageData


class JobItemPageData(BaseModel):
    items: list[dict[str, Any]]
    pagination: Pagination


class JobItemPageResponse(SuccessEnvelope):
    data: JobItemPageData


class JobControlData(BaseModel):
    job_id: UUID
    status: JobStatus
    version: int
    allowed_actions: list[str]


class JobControlResponse(SuccessEnvelope):
    data: JobControlData


def idempotency_key(
    value: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=160),
    ],
) -> str:
    key = value.strip()
    if not key:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="任务写操作需要有效的幂等键",
            status_code=422,
        )
    return key


IdempotencyKey = Annotated[str, Depends(idempotency_key)]


@router.get("", response_model=JobPageResponse)
async def list_jobs(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    status: JobStatus | None = None,
    job_type: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    queue: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> dict[str, Any]:
    if (
        created_from is not None
        and created_to is not None
        and created_from > created_to
    ):
        raise AppError(
            code="JOB_FILTER_INVALID",
            message="任务创建时间范围无效",
            status_code=422,
        )
    result = await application.list_jobs(
        page=page,
        page_size=page_size,
        status=status,
        job_type=job_type,
        queue=queue,
        created_from=created_from,
        created_to=created_to,
    )
    return success_response(
        data={
            "items": [_job(row) for row in result.items],
            "pagination": {
                "page": result.page,
                "page_size": result.page_size,
                "total": result.total,
            },
        }
    )


@router.get("/{job_id}")
async def get_job(
    job_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data=_job(await application.get_job(job_id), detail=True))


@router.get("/{job_id}/runs")
async def list_job_runs(
    job_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(
        data={"items": [_run(row) for row in await application.list_runs(job_id)]}
    )


@router.get("/{job_id}/items", response_model=JobItemPageResponse)
async def list_job_items(
    job_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    status: JobItemStatus | None = None,
) -> dict[str, Any]:
    rows, total = await application.list_items(
        job_id, page=page, page_size=page_size, status=status
    )
    return success_response(
        data={
            "items": [_item(row) for row in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.get("/{job_id}/allowed-actions")
async def get_allowed_actions(
    job_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(
        data={
            "job_id": str(job_id),
            "allowed_actions": list(await application.allowed_actions(job_id)),
        }
    )


def _command_route(action: JobAction):
    async def endpoint(
        job_id: UUID,
        body: JobCommandBody,
        application: Application,
        identity: WriteIdentity,
        key: IdempotencyKey,
    ) -> dict[str, Any]:
        if not body.confirm:
            raise AppError(
                code="AUTH_CONFIRMATION_REQUIRED",
                message="请确认本次任务控制操作",
                status_code=422,
            )
        reason = body.reason.strip()
        if not reason:
            raise AppError(
                code="JOB_INPUT_INVALID",
                message="操作原因不能为空",
                status_code=422,
            )
        audit = identity.audit_context
        session = getattr(identity, "session", None)
        job = await application.command(
            job_id,
            action,
            JobCommandContext(
                request_id=audit.request_id,
                idempotency_key=key,
                actor_user_id=str(identity.user.id),
                reason=reason,
                expected_version=body.expected_version,
                session_id=str(session.id) if session is not None else None,
                trusted_ip=getattr(audit, "trusted_ip", None),
            ),
        )
        return success_response(
            data={
                "job_id": str(job.id),
                "status": str(job.status),
                "version": job.version,
                "allowed_actions": list(await application.allowed_actions(job.id)),
            },
            code="JOB_ACCEPTED",
            message="任务控制请求已受理",
        )

    return endpoint


for _action in ("cancel", "pause", "resume", "retry", "retry-failed-items"):
    router.add_api_route(
        f"/{{job_id}}/{_action}",
        _command_route(_action),
        methods=["POST"],
        status_code=202,
        response_model=JobControlResponse,
        name=f"job_{_action.replace('-', '_')}",
    )


def _job(job: Any, *, detail: bool = False) -> dict[str, Any]:
    value = {
        "id": str(job.id),
        "job_type": job.job_type,
        "business_object_type": job.business_object_type,
        "business_object_id": job.business_object_id,
        "queue": job.queue,
        "priority": job.priority,
        "status": str(job.status),
        "progress": job.progress,
        "result_summary": job.result_summary,
        "current_run_id": str(job.current_run_id) if job.current_run_id else None,
        "version": job.version,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "terminal_at": job.terminal_at,
    }
    if detail:
        value["config_snapshot"] = job.config_snapshot
        value["request_id"] = job.request_id
        value["created_by_user_id"] = job.created_by_user_id
        value["soft_timeout_seconds"] = job.soft_timeout_seconds
        value["hard_timeout_seconds"] = job.hard_timeout_seconds
    return value


def _run(run: Any) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "job_id": str(run.job_id),
        "attempt_no": run.attempt_no,
        "worker_id": run.worker_id,
        "status": str(run.status),
        "claimed_at": run.claimed_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "heartbeat_at": run.heartbeat_at,
        "exit_type": run.exit_type,
        "error_code": run.error_code,
        "error_summary": run.error_summary,
        "metrics": run.metrics,
    }


def _item(item: Any) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "job_id": str(item.job_id),
        "item_key": item.item_key,
        "status": str(item.status),
        "attempt_count": item.attempt_count,
        "result_ref": item.result_ref,
        "error_code": item.error_code,
        "created_at": item.created_at,
        "started_at": item.started_at,
        "ended_at": item.ended_at,
        "updated_at": item.updated_at,
    }
