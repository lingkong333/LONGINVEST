from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.alerts.application import (
    AlertApplication,
    get_alert_application,
)
from long_invest.modules.alerts.contracts import (
    AlertCommand,
    AlertSeverity,
    AlertStatus,
)
from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])
Application = Annotated[AlertApplication, Depends(get_alert_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]
AlertStatusFilter = Annotated[AlertStatus | None, Query(alias="status")]


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    confirm: StrictBool


@router.get("", response_model=SuccessEnvelope)
async def list_alerts(
    application: Application,
    _identity: ReadIdentity,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status_filter: AlertStatusFilter = None,
    severity: AlertSeverity | None = None,
    alert_type: str | None = Query(None, max_length=100),
):
    items, total = await application.read(
        "list",
        status=status_filter,
        severity=severity,
        alert_type=alert_type,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data={
            "items": [_alert(item) for item in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/{alert_id}", response_model=SuccessEnvelope)
async def get_alert(alert_id: UUID, application: Application, _identity: ReadIdentity):
    return success_response(data=_alert(await application.read("get", alert_id)))


@router.get("/{alert_id}/occurrences", response_model=SuccessEnvelope)
async def list_occurrences(
    alert_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    items, total = await application.read(
        "occurrences", alert_id, page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [_occurrence(item) for item in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/{alert_id}/actions", response_model=SuccessEnvelope)
async def list_actions(
    alert_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    items, total = await application.read(
        "actions", alert_id, page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [_action(item) for item in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.post("/{alert_id}/acknowledge", response_model=SuccessEnvelope)
async def acknowledge(
    alert_id: UUID,
    body: ActionRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    alert, replayed = await application.write(
        "acknowledge", _command(alert_id, body, identity, idempotency_key)
    )
    return success_response(data={**_alert(alert), "replayed": replayed})


@router.post("/{alert_id}/resolve", response_model=SuccessEnvelope)
async def resolve(
    alert_id: UUID,
    body: ActionRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    alert, replayed = await application.write(
        "resolve", _command(alert_id, body, identity, idempotency_key)
    )
    return success_response(data={**_alert(alert), "replayed": replayed})


@router.post(
    "/{alert_id}/retry",
    response_model=SuccessEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry(
    alert_id: UUID,
    body: ActionRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    alert, job_id, replayed = await application.write(
        "retry", _command(alert_id, body, identity, idempotency_key)
    )
    return success_response(
        data={"alert": _alert(alert), "job_id": job_id, "replayed": replayed},
        code="JOB_ACCEPTED",
        message="重试任务已创建",
    )


def _command(alert_id, body, identity, idempotency_key):
    return AlertCommand(
        alert_id=alert_id,
        expected_version=body.expected_version,
        reason=body.reason,
        request_id=identity.audit_context.request_id,
        idempotency_key=idempotency_key,
        actor_user_id=str(identity.user.id),
        session_id=str(identity.session.id),
        trusted_ip=identity.audit_context.trusted_ip or "unknown",
    )


def _confirm(value):
    if not value:
        raise AppError(
            code="ALERT_CONFIRMATION_REQUIRED",
            message="请确认本次告警操作",
            status_code=422,
        )


def _alert(item):
    fields = (
        "id",
        "aggregation_key",
        "alert_type",
        "object_type",
        "object_id",
        "severity",
        "status",
        "title",
        "summary",
        "details",
        "occurrence_count",
        "first_seen_at",
        "last_seen_at",
        "acknowledged_at",
        "acknowledged_by_user_id",
        "resolved_at",
        "resolved_by_user_id",
        "resolution_reason",
        "version",
        "created_at",
        "updated_at",
    )
    return {field: getattr(item, field) for field in fields}


def _occurrence(item):
    fields = (
        "id",
        "alert_id",
        "source_event_id",
        "severity",
        "summary",
        "details",
        "request_id",
        "occurred_at",
    )
    return {field: getattr(item, field) for field in fields}


def _action(item):
    fields = (
        "id",
        "alert_id",
        "action",
        "reason",
        "actor_user_id",
        "request_id",
        "job_id",
        "created_at",
    )
    return {field: getattr(item, field) for field in fields}
