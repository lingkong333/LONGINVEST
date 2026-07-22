from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
)
from long_invest.modules.dashboard.application import DashboardApplication
from long_invest.modules.dashboard.contracts import (
    DashboardStatus,
    DashboardSummary,
    DashboardTimeline,
    SectionStatus,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


_application_factory: Callable[[], DashboardApplication] | None = None


def configure_dashboard_application(
    factory: Callable[[], DashboardApplication],
) -> None:
    global _application_factory
    _application_factory = factory


def get_dashboard_application() -> DashboardApplication:
    if _application_factory is None:
        raise AppError(
            code="DASHBOARD_NOT_CONFIGURED",
            message="仪表盘尚未完成生产装配",
            status_code=503,
        )
    return _application_factory()


Application = Annotated[DashboardApplication, Depends(get_dashboard_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]


class SectionData(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SystemData(SectionData):
    open_alerts: int | None = None
    critical_alerts: int | None = None


class QuoteBatchData(SectionData):
    is_trading_day: bool | None = None
    starts_at: time | None = None
    local_time: time | None = None
    status: str | None = None
    expected_count: int | None = None
    valid_count: int | None = None
    missing_count: int | None = None
    conflict_count: int | None = None
    failed_count: int | None = None


class MonitoringData(SectionData):
    active: int | None = None
    with_current_state: int | None = None
    missing_state: int | None = None


class PositionData(SectionData):
    held: int | None = None
    high_zone: int | None = None


class SignalData(SectionData):
    today: int | None = None
    low_zone: int | None = None
    high_zone: int | None = None


class DailyData(SectionData):
    trading_date: date | None = None
    status: str | None = None
    expected_count: int | None = None
    committed_count: int | None = None
    missing_count: int | None = None
    failed_count: int | None = None


class TargetData(SectionData):
    total: int | None = None
    active: int | None = None
    attention: int | None = None


class JobData(SectionData):
    active: int | None = None
    failed: int | None = None
    timed_out: int | None = None


class NotificationData(SectionData):
    pending: int | None = None
    sent: int | None = None
    failed: int | None = None


class ProviderData(SectionData):
    total: int | None = None
    healthy: int | None = None
    open_circuits: int | None = None


class InfrastructureData(SectionData):
    stale_workers: int | None = None
    active_workers: int | None = None
    calendar_covers_today: bool | None = None


class AlertData(SectionData):
    unresolved: int | None = None
    critical: int | None = None
    errors: int | None = None


class DashboardSectionResponse[SectionDataType: SectionData](BaseModel):
    status: SectionStatus
    updated_at: datetime
    data: SectionDataType
    error: str | None


class DashboardSectionsResponse(BaseModel):
    system: DashboardSectionResponse[SystemData]
    quote_batches: DashboardSectionResponse[QuoteBatchData]
    monitoring: DashboardSectionResponse[MonitoringData]
    positions: DashboardSectionResponse[PositionData]
    signals: DashboardSectionResponse[SignalData]
    daily_data: DashboardSectionResponse[DailyData]
    targets: DashboardSectionResponse[TargetData]
    jobs: DashboardSectionResponse[JobData]
    notifications: DashboardSectionResponse[NotificationData]
    providers: DashboardSectionResponse[ProviderData]
    infrastructure: DashboardSectionResponse[InfrastructureData]
    alerts: DashboardSectionResponse[AlertData]


class DashboardSummaryResponse(BaseModel):
    status: DashboardStatus
    generated_at: datetime
    sections: DashboardSectionsResponse


class DashboardSummaryEnvelope(SuccessEnvelope):
    data: DashboardSummaryResponse


class TimelineItemResponse(BaseModel):
    id: str
    event_type: str
    object_type: str
    object_id: str
    title: str
    occurred_at: datetime
    details: dict[str, str | int | float | Decimal | bool | None]


class DashboardTimelineResponse(BaseModel):
    items: list[TimelineItemResponse]
    generated_at: datetime


class DashboardTimelineEnvelope(SuccessEnvelope):
    data: DashboardTimelineResponse


@router.get("/summary", response_model=DashboardSummaryEnvelope)
async def get_dashboard_summary(
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    return success_response(data=_summary(await application.summary()))


@router.get("/timeline", response_model=DashboardTimelineEnvelope)
async def get_dashboard_timeline(
    application: Application,
    _identity: ReadIdentity,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
) -> dict[str, Any]:
    return success_response(
        data=_timeline(await application.timeline(limit=limit, before=before))
    )


def _summary(summary: DashboardSummary) -> dict[str, Any]:
    return {
        "status": summary.status,
        "generated_at": summary.generated_at,
        "sections": {
            name: {
                "status": section.status,
                "updated_at": section.updated_at,
                "data": section.data,
                "error": section.error,
            }
            for name, section in summary.sections.items()
        },
    }


def _timeline(timeline: DashboardTimeline) -> dict[str, Any]:
    return {
        "items": [
            {
                "id": item.id,
                "event_type": item.event_type,
                "object_type": item.object_type,
                "object_id": item.object_id,
                "title": item.title,
                "occurred_at": item.occurred_at,
                "details": item.details,
            }
            for item in timeline.items
        ],
        "generated_at": timeline.generated_at,
    }
