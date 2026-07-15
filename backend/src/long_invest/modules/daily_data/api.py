from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.daily_data.application import (
    DailyDataApplication,
    get_daily_data_application,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.request_id import get_request_id
from long_invest.platform.http.responses import success_response

router = APIRouter(tags=["daily-data"])
Application = Annotated[DailyDataApplication, Depends(get_daily_data_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


@router.get("/api/v1/daily-data/batches")
async def list_batches(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_batches(page=page, page_size=page_size)
    return _page(items, page=page, page_size=page_size, total=total)


@router.get("/api/v1/daily-data/batches/{batch_id}/missing")
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


@router.post("/api/v1/daily-data/batches/{batch_id}/retry", status_code=202)
async def retry_batch(
    batch_id: UUID,
    body: RetryRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=160)
    ] = None,
) -> dict[str, Any]:
    if not body.confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认重试日线缺失股票",
            status_code=422,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="日线重试需要幂等键",
            status_code=422,
        )
    job = await application.retry(
        batch_id=batch_id,
        idempotency_key=idempotency_key.strip(),
        request_id=get_request_id(),
        created_by_user_id=str(identity.user.id),
    )
    return success_response(
        data={"job_id": str(job.id), "job_type": job.job_type, "status": job.status},
        message="日线缺失重试任务已创建",
    )


@router.get("/api/v1/daily-bars/{symbol}/revisions")
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


@router.get("/api/v1/daily-bars/{symbol}")
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
