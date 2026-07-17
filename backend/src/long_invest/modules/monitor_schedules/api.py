from __future__ import annotations

from datetime import datetime, time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.monitor_schedules.application import (
    MonitorScheduleApplication,
    get_monitor_schedule_application,
)
from long_invest.modules.monitor_schedules.contracts import ScheduleDefinition
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(prefix="/api/v1/monitor-schedules", tags=["monitor-schedules"])
Application = Annotated[
    MonitorScheduleApplication, Depends(get_monitor_schedule_application)
]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateScheduleRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=100)
    times: tuple[time, ...] = ()
    reason: str = Field(min_length=1, max_length=500)
    confirm: StrictBool


class UpdateScheduleRequest(CreateScheduleRequest):
    expected_version: int = Field(ge=1)


class ArchiveScheduleRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    confirm: StrictBool


class RestoreScheduleRequest(ArchiveScheduleRequest):
    pass


class ScheduleRecord(BaseModel):
    id: UUID
    name: str
    current_revision_id: UUID | None
    version: int
    archived_at: datetime | None


class ScheduleRevisionRecord(BaseModel):
    id: UUID
    schedule_id: UUID
    revision_no: int
    times: list[str]
    timezone: str
    reason: str
    created_at: datetime


class ScheduleListData(BaseModel):
    items: list[ScheduleRecord]


class ScheduleDetailData(BaseModel):
    schedule: ScheduleRecord
    revision: ScheduleRevisionRecord


class ScheduleMutationData(ScheduleDetailData):
    replayed: bool


class ScheduleRevisionListData(BaseModel):
    items: list[ScheduleRevisionRecord]


class ScheduleListResponse(SuccessEnvelope):
    data: ScheduleListData


class ScheduleDetailResponse(SuccessEnvelope):
    data: ScheduleDetailData


class ScheduleMutationResponse(SuccessEnvelope):
    data: ScheduleMutationData


class ScheduleRevisionListResponse(SuccessEnvelope):
    data: ScheduleRevisionListData


@router.get("", response_model=ScheduleListResponse)
async def list_schedules(
    application: Application,
    _identity: ReadIdentity,
    include_archived: bool = Query(False),
) -> dict[str, Any]:
    return success_response(
        data={
            "items": [
                item.model_dump(mode="json")
                for item in await application.list(include_archived=include_archived)
            ]
        }
    )


@router.post("", response_model=ScheduleMutationResponse)
async def create_schedule(
    body: CreateScheduleRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body.confirm)
    result = await application.create(
        ScheduleDefinition(
            name=body.name,
            times=body.times,
            reason=body.reason,
            idempotency_key=idempotency_key,
        ),
        **_context(identity),
    )
    return success_response(data=_result(result))


@router.get("/{schedule_id}", response_model=ScheduleDetailResponse)
async def get_schedule(
    schedule_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    schedule = await application.get(schedule_id)
    revisions = await application.versions(schedule_id)
    revision = next(
        item for item in revisions if item.id == schedule.current_revision_id
    )
    return success_response(
        data={
            "schedule": schedule.model_dump(mode="json"),
            "revision": _revision_data(revision),
        }
    )


@router.patch("/{schedule_id}", response_model=ScheduleMutationResponse)
async def update_schedule(
    schedule_id: UUID,
    body: UpdateScheduleRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body.confirm)
    result = await application.update(
        schedule_id,
        ScheduleDefinition(
            name=body.name,
            times=body.times,
            reason=body.reason,
            idempotency_key=idempotency_key,
            expected_version=body.expected_version,
        ),
        **_context(identity),
    )
    return success_response(data=_result(result))


@router.post("/{schedule_id}/archive", response_model=ScheduleMutationResponse)
async def archive_schedule(
    schedule_id: UUID,
    body: ArchiveScheduleRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body.confirm)
    result = await application.archive(
        schedule_id,
        expected_version=body.expected_version,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(data=_result(result))


@router.get("/{schedule_id}/versions", response_model=ScheduleRevisionListResponse)
async def list_versions(
    schedule_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(
        data={
            "items": [
                _revision_data(item) for item in await application.versions(schedule_id)
            ]
        }
    )


@router.post(
    "/{schedule_id}/versions/{revision_id}/restore",
    response_model=ScheduleMutationResponse,
)
async def restore_schedule(
    schedule_id: UUID,
    revision_id: UUID,
    body: RestoreScheduleRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body.confirm)
    result = await application.restore(
        schedule_id,
        source_revision_id=revision_id,
        expected_version=body.expected_version,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(data=_result(result))


def _confirm(value: bool) -> None:
    if not value:
        raise AppError(
            code="MONITOR_SCHEDULE_CONFIRMATION_REQUIRED",
            message="请确认调度修改",
            status_code=422,
        )


def _context(identity: AuthenticatedRequest) -> dict[str, str]:
    return {
        "request_id": identity.audit_context.request_id,
        "actor_user_id": str(identity.user.id),
        "session_id": str(identity.session.id),
        "trusted_ip": identity.audit_context.trusted_ip or "unknown",
    }


def _result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schedule": result["schedule"].model_dump(mode="json"),
        "revision": _revision_data(result["revision"]),
        "replayed": result["replayed"],
    }


def _revision_data(revision: Any) -> dict[str, Any]:
    return {
        "id": revision.id,
        "schedule_id": revision.schedule_id,
        "revision_no": revision.revision_no,
        "times": [value.strftime("%H:%M") for value in revision.times],
        "timezone": revision.timezone,
        "reason": revision.reason,
        "created_at": revision.created_at,
    }
