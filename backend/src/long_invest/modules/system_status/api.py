from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
)
from long_invest.modules.system_status.application import SystemStatusApplication
from long_invest.modules.system_status.contracts import (
    ComponentStatus,
    QueueStatus,
    ScheduleOccurrence,
    SchedulerStatus,
    SystemClockStatus,
    SystemHealth,
    WorkerStatus,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(tags=["system-status"])
_application_factory: Callable[[], SystemStatusApplication] | None = None


def configure_system_status_application(
    factory: Callable[[], SystemStatusApplication],
) -> None:
    global _application_factory
    _application_factory = factory


def get_system_status_application() -> SystemStatusApplication:
    if _application_factory is None:
        raise AppError(
            code="SYSTEM_STATUS_NOT_CONFIGURED",
            message="系统运行状态尚未完成生产装配",
            status_code=503,
        )
    return _application_factory()


Application = Annotated[SystemStatusApplication, Depends(get_system_status_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]


class SystemHealthEnvelope(SuccessEnvelope):
    data: SystemHealth


class ComponentListData(BaseModel):
    items: list[ComponentStatus]


class ComponentListEnvelope(SuccessEnvelope):
    data: ComponentListData


class WorkerListData(BaseModel):
    items: list[WorkerStatus]


class WorkerListEnvelope(SuccessEnvelope):
    data: WorkerListData


class QueueListData(BaseModel):
    items: list[QueueStatus]


class QueueListEnvelope(SuccessEnvelope):
    data: QueueListData


class SchedulerStatusEnvelope(SuccessEnvelope):
    data: SchedulerStatus


class OccurrenceListData(BaseModel):
    items: list[ScheduleOccurrence]
    pagination: Pagination


class OccurrenceListEnvelope(SuccessEnvelope):
    data: OccurrenceListData


class SystemClockStatusEnvelope(SuccessEnvelope):
    data: SystemClockStatus


@router.get("/api/v1/system/health", response_model=SystemHealthEnvelope)
async def system_health(
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    value = await application.get_health()
    return success_response(data=value.model_dump(mode="json"))


@router.get("/api/v1/system/components", response_model=ComponentListEnvelope)
async def system_components(
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    items = await application.list_components()
    return success_response(
        data={"items": [item.model_dump(mode="json") for item in items]}
    )


@router.get("/api/v1/workers", response_model=WorkerListEnvelope)
async def workers(
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    items = await application.list_workers()
    return success_response(
        data={"items": [item.model_dump(mode="json") for item in items]}
    )


@router.get("/api/v1/queues", response_model=QueueListEnvelope)
async def queues(
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    items = await application.list_queues()
    return success_response(
        data={"items": [item.model_dump(mode="json") for item in items]}
    )


@router.get("/api/v1/scheduler/status", response_model=SchedulerStatusEnvelope)
async def scheduler_status(
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    value = await application.get_scheduler_status()
    return success_response(data=value.model_dump(mode="json"))


@router.get("/api/v1/schedule-occurrences", response_model=OccurrenceListEnvelope)
async def schedule_occurrences(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    occurrence_type: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    status: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    from_date: date | None = None,
    through_date: date | None = None,
) -> dict[str, Any]:
    result = await application.list_occurrences(
        page=page,
        page_size=page_size,
        occurrence_type=occurrence_type,
        status=status,
        from_date=from_date,
        through_date=through_date,
    )
    return success_response(
        data={
            "items": [item.model_dump(mode="json") for item in result.items],
            "pagination": {
                "page": result.page,
                "page_size": result.page_size,
                "total": result.total,
            },
        }
    )


@router.get("/api/v1/system-clock/status", response_model=SystemClockStatusEnvelope)
async def system_clock_status(
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    value = await application.get_clock_status()
    return success_response(data=value.model_dump(mode="json"))
