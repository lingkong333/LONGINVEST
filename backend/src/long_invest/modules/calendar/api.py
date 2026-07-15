from collections.abc import AsyncIterator
from datetime import date as Date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.calendar.contracts import (
    CalendarDayInput,
    CalendarImport,
    OverrideCalendarDay,
    RestoreCalendarVersion,
    TradingSessionInput,
)
from long_invest.modules.calendar.repository import CalendarRepository
from long_invest.modules.calendar.service import TradingCalendarService
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import get_database
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response

router = APIRouter(prefix="/api/v1/trading-calendar", tags=["trading-calendar"])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportRequest(StrictRequest):
    market: str = Field(min_length=1, max_length=16)
    source: str = Field(min_length=1, max_length=64)
    source_version: str = Field(min_length=1, max_length=128)
    expected_current_version: int | None = Field(default=None, ge=1)
    days: tuple[CalendarDayInput, ...]
    reason: str = Field(min_length=1, max_length=500)
    confirm: bool


class OverrideRequest(StrictRequest):
    market: str = "CN_A"
    is_trading_day: bool
    sessions: tuple[TradingSessionInput, ...] = ()
    expected_current_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    confirm: bool
    note: str | None = Field(default=None, max_length=500)


class RestoreRequest(StrictRequest):
    market: str = "CN_A"
    expected_current_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    confirm: bool


async def get_calendar_service() -> AsyncIterator[TradingCalendarService]:
    async with get_database().transaction() as session:
        yield TradingCalendarService(
            CalendarRepository(session),
            audit_service=AuditService(session),
        )


ServiceDependency = Annotated[TradingCalendarService, Depends(get_calendar_service)]
ReadAuth = Annotated[
    AuthenticatedRequest, Depends(require_authenticated_request)
]
WriteAuth = Annotated[
    AuthenticatedRequest, Depends(require_verified_write_request)
]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]


@router.get("")
async def list_calendar(
    service: ServiceDependency,
    _authenticated: ReadAuth,
    from_date: Annotated[Date, Query(alias="from")],
    through_date: Annotated[Date, Query(alias="through")],
    market: str = "CN_A",
) -> dict:
    items = await service.list_days(from_date, through_date, market)
    return success_response(data={"items": [_day_data(item) for item in items]})


@router.get("/coverage")
async def calendar_coverage(
    service: ServiceDependency,
    _authenticated: ReadAuth,
    from_date: Annotated[Date, Query(alias="from")],
    market: str = "CN_A",
) -> dict:
    value = await service.coverage(from_date, market)
    return success_response(data=value.model_dump(mode="json"))


@router.get("/next-trading-day")
async def next_trading_day(
    service: ServiceDependency,
    _authenticated: ReadAuth,
    after: Date,
    market: str = "CN_A",
) -> dict:
    return success_response(
        data=_day_data(await service.next_trading_day(after, market))
    )


@router.get("/previous-trading-day")
async def previous_trading_day(
    service: ServiceDependency,
    _authenticated: ReadAuth,
    before: Date,
    market: str = "CN_A",
) -> dict:
    return success_response(
        data=_day_data(await service.previous_trading_day(before, market))
    )


@router.get("/versions")
async def versions(
    service: ServiceDependency,
    _authenticated: ReadAuth,
    market: str = "CN_A",
) -> dict:
    items = await service.list_versions(market)
    return success_response(data={"items": [_version_data(item) for item in items]})


@router.get("/{date}")
async def calendar_day(
    date: Date,
    service: ServiceDependency,
    _authenticated: ReadAuth,
    market: str = "CN_A",
) -> dict:
    item = await service.get_day(date, market)
    if item is None:
        raise AppError(
            code="CALENDAR_DATE_NOT_FOUND",
            message="日历日期不存在",
            status_code=404,
        )
    return success_response(data=_day_data(item))


@router.patch("/{date}")
async def override_calendar_day(
    date: Date,
    body: OverrideRequest,
    service: ServiceDependency,
    _authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    result = await service.override_day(
        OverrideCalendarDay(
            market=body.market,
            trade_date=date,
            is_trading_day=body.is_trading_day,
            sessions=body.sessions,
            expected_current_version=body.expected_current_version,
            reason=body.reason,
            idempotency_key=idempotency_key,
            note=body.note,
        )
    )
    return success_response(data=result.model_dump(mode="json"))


@router.post("/import")
async def import_calendar(
    body: ImportRequest,
    service: ServiceDependency,
    _authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    result = await service.import_version(
        CalendarImport(
            market=body.market,
            source=body.source,
            source_version=body.source_version,
            idempotency_key=idempotency_key,
            expected_current_version=body.expected_current_version,
            days=body.days,
            reason=body.reason,
        )
    )
    return success_response(data=result.model_dump(mode="json"))


@router.post("/versions/{version_id}/restore")
async def restore_calendar(
    version_id: UUID,
    body: RestoreRequest,
    service: ServiceDependency,
    _authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    result = await service.restore_version(
        RestoreCalendarVersion(
            market=body.market,
            version_id=version_id,
            expected_current_version=body.expected_current_version,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
    )
    return success_response(data=result.model_dump(mode="json"))


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="CALENDAR_CONFIRMATION_REQUIRED",
            message="请确认日历修改操作",
            status_code=422,
        )


def _day_data(item) -> dict | None:
    if item is None:
        return None
    return {
        "trade_date": item.trade_date,
        "is_trading_day": item.is_trading_day,
        "status": item.status,
        "source": item.source,
        "note": item.note,
        "override_reason": item.override_reason,
        "sessions": [
            {"starts_at": value.starts_at, "ends_at": value.ends_at}
            for value in item.sessions
        ],
    }


def _version_data(item) -> dict:
    return {
        "id": str(item.id),
        "market": item.market,
        "version_number": item.version_number,
        "source": item.source,
        "source_version": item.source_version,
        "based_on_version_id": str(item.based_on_version_id)
        if item.based_on_version_id
        else None,
        "reason": item.reason,
        "created_at": item.created_at,
    }
