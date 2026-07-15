from collections.abc import AsyncIterator
from datetime import date as Date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.calendar.contracts import (
    CalendarAuditContext,
    CalendarDayInput,
    CalendarImport,
    CalendarVersionResult,
    OverrideCalendarDay,
    RestoreCalendarVersion,
    TradingSessionInput,
)
from long_invest.modules.calendar.outbox import CalendarOutboxAdapter
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
    sessions: tuple[TradingSessionInput, ...] | None = None
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
    try:
        async with get_database().transaction() as session:
            yield TradingCalendarService(
                CalendarRepository(session),
                audit_service=AuditService(session),
                event_sink=CalendarOutboxAdapter(session),
            )
    except SQLAlchemyError as exc:
        raise AppError(
            code="CALENDAR_BACKEND_UNAVAILABLE",
            message="交易日历服务暂时不可用",
            status_code=503,
        ) from exc


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
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    override_data = {
        "market": body.market,
        "trade_date": date,
        "is_trading_day": body.is_trading_day,
        "expected_current_version": body.expected_current_version,
        "reason": body.reason,
        "idempotency_key": idempotency_key,
        "note": body.note,
        "audit_context": _calendar_context(authenticated, idempotency_key),
    }
    if body.sessions is not None:
        override_data["sessions"] = body.sessions
    result = await service.override_day(
        OverrideCalendarDay.model_validate(override_data)
    )
    return success_response(data=_result_data(result))


@router.post("/import")
async def import_calendar(
    body: ImportRequest,
    service: ServiceDependency,
    authenticated: WriteAuth,
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
            audit_context=_calendar_context(authenticated, idempotency_key),
        )
    )
    return success_response(data=_result_data(result))


@router.post("/versions/{version_id}/restore")
async def restore_calendar(
    version_id: UUID,
    body: RestoreRequest,
    service: ServiceDependency,
    authenticated: WriteAuth,
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
            audit_context=_calendar_context(authenticated, idempotency_key),
        )
    )
    return success_response(data=_result_data(result))


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="CALENDAR_CONFIRMATION_REQUIRED",
            message="请确认日历修改操作",
            status_code=422,
        )


def _calendar_context(
    authenticated: AuthenticatedRequest,
    idempotency_key: str,
) -> CalendarAuditContext:
    return CalendarAuditContext(
        request_id=authenticated.audit_context.request_id,
        idempotency_key=idempotency_key,
        actor_user_id=str(authenticated.user.id),
        session_id=str(authenticated.session.id),
        trusted_ip=authenticated.audit_context.trusted_ip or "unknown",
    )


def _result_data(result: CalendarVersionResult) -> dict:
    if result.issues:
        raise AppError(
            code="CALENDAR_CONTENT_INVALID",
            message="日历内容校验失败",
            status_code=422,
            details={
                "issues": [item.model_dump(mode="json") for item in result.issues]
            },
        )
    return result.model_dump(mode="json")


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
