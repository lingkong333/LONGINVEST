from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.daily_data.application import (
    DailyDataApplication,
    get_daily_data_application,
)
from long_invest.modules.daily_data.contracts import DailyRetryAuditContext
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if not value:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="日线重试需要幂等键",
            status_code=422,
        )
    if len(value) > 160:
        raise AppError(
            code="VALIDATION_ERROR",
            message="幂等键不能超过 160 个字符",
            status_code=422,
        )
    return value


router = APIRouter(tags=["daily-data"])
Application = Annotated[DailyDataApplication, Depends(get_daily_data_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[str, Depends(_idempotency_key)]


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class DailyBatchRecord(BaseModel):
    id: UUID
    trading_date: date
    universe_snapshot_id: UUID
    parent_batch_id: UUID | None
    symbols: list[str]
    security_ids: list[str]
    known_corporate_action_symbols: list[str]
    idempotency_key: str
    status: str
    expected_count: int
    fetched_count: int
    validated_count: int
    committed_count: int
    missing_count: int
    failed_count: int
    created_at: datetime
    started_at: datetime | None
    deadline_at: datetime | None
    completed_at: datetime | None


class DailyMissingRecord(BaseModel):
    id: UUID
    batch_id: UUID
    security_id: UUID | None
    symbol: str
    reason: str
    error_code: str | None
    explained: bool
    created_at: datetime


class DailyBarRecord(BaseModel):
    security_id: UUID
    trade_date: date
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    previous_close: Decimal | None
    volume: int
    amount: Decimal
    source: str
    data_version: int
    created_at: datetime
    updated_at: datetime


class DailyRevisionRecord(BaseModel):
    id: UUID
    daily_bar_security_id: UUID
    daily_bar_trade_date: date
    symbol: str
    revision_no: int
    old_values: dict[str, Any]
    new_values: dict[str, Any]
    changed_fields: list[str]
    source: str
    reason: str
    created_at: datetime


class DailyJobData(BaseModel):
    job_id: UUID
    job_type: str
    status: str


class DailyBatchPageData(BaseModel):
    items: list[DailyBatchRecord]
    pagination: Pagination


class DailyMissingPageData(BaseModel):
    items: list[DailyMissingRecord]
    pagination: Pagination


class DailyBarPageData(BaseModel):
    items: list[DailyBarRecord]
    pagination: Pagination


class DailyRevisionPageData(BaseModel):
    items: list[DailyRevisionRecord]
    pagination: Pagination


class DailyBatchPageResponse(SuccessEnvelope):
    data: DailyBatchPageData


class DailyMissingPageResponse(SuccessEnvelope):
    data: DailyMissingPageData


class DailyBarPageResponse(SuccessEnvelope):
    data: DailyBarPageData


class DailyRevisionPageResponse(SuccessEnvelope):
    data: DailyRevisionPageData


class DailyJobResponse(SuccessEnvelope):
    data: DailyJobData


@router.get(
    "/api/v1/daily-data/batches",
    response_model=DailyBatchPageResponse,
)
async def list_batches(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_batches(page=page, page_size=page_size)
    return _page(items, page=page, page_size=page_size, total=total)


@router.get(
    "/api/v1/daily-data/batches/{batch_id}/missing",
    response_model=DailyMissingPageResponse,
)
async def list_missing(
    batch_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_missing(
        batch_id, page=page, page_size=page_size
    )
    return _page(items, page=page, page_size=page_size, total=total)


@router.post(
    "/api/v1/daily-data/batches/{batch_id}/retry",
    status_code=202,
    response_model=DailyJobResponse,
    openapi_extra={
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 160},
            }
        ]
    },
)
async def retry_batch(
    batch_id: UUID,
    body: RetryRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    if not body.confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认重试日线缺失股票",
            status_code=422,
        )
    job = await application.retry(
        batch_id=batch_id,
        audit_context=DailyRetryAuditContext(
            request_id=identity.audit_context.request_id,
            idempotency_key=idempotency_key,
            actor_user_id=str(identity.user.id),
            session_id=str(identity.session.id),
            trusted_ip=identity.audit_context.trusted_ip or "unknown",
            reason=body.reason.strip(),
        ),
    )
    return success_response(
        data={"job_id": str(job.id), "job_type": job.job_type, "status": job.status},
        code="JOB_ACCEPTED",
        message="日线缺失重试任务已创建",
    )


@router.get(
    "/api/v1/daily-bars/{symbol}/revisions",
    response_model=DailyRevisionPageResponse,
)
async def list_revisions(
    symbol: str,
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_revisions(
        symbol, page=page, page_size=page_size
    )
    return _page(items, page=page, page_size=page_size, total=total)


@router.get(
    "/api/v1/daily-bars/{symbol}",
    response_model=DailyBarPageResponse,
)
async def list_bars(
    symbol: str,
    application: Application,
    _identity: ReadIdentity,
    start: date,
    end: date,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    items, total = await application.list_bars(
        symbol, start=start, end=end, page=page, page_size=page_size
    )
    return _page(items, page=page, page_size=page_size, total=total)


def _page(items, *, page: int, page_size: int, total: int) -> dict[str, Any]:
    return success_response(
        data={
            "items": [_record(item) for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


def _record(item: object) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in item.__table__.columns if hasattr(item, "__table__") else vars(item):
        name = column.name if hasattr(column, "name") else column
        value = getattr(item, name)
        if isinstance(value, Decimal):
            value = str(value)
        result[name] = value
    return result
