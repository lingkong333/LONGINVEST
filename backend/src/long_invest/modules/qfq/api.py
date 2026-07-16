from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.qfq.application import QfqApplication, get_qfq_application
from long_invest.modules.qfq.contracts import QfqDatasetLifecycle, QfqFreshness
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(prefix="/api/v1/qfq-data", tags=["qfq-data"])
Application = Annotated[QfqApplication, Depends(get_qfq_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if not value:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="前复权刷新需要幂等键",
            status_code=422,
        )
    if len(value) > 160:
        raise AppError(
            code="QFQ_WINDOW_INVALID",
            message="幂等键不能超过 160 个字符",
            status_code=422,
        )
    return value


IdempotencyKey = Annotated[str, Depends(_idempotency_key)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RefreshRequest(StrictRequest):
    start: date
    end: date
    as_of_date: date
    confirm: StrictBool
    reason: Annotated[str, Field(min_length=1, max_length=64)]


class QfqDatasetRecord(BaseModel):
    id: UUID
    security_id: UUID
    symbol: str
    version: int
    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    as_of_date: date
    provider: str
    provider_contract_version: str
    anchor_date: date
    anchor_close: str
    row_count: int
    checksum: str
    lifecycle: QfqDatasetLifecycle
    freshness: QfqFreshness
    stale_reason: str | None
    created_at: datetime
    activated_at: datetime | None
    superseded_at: datetime | None


class QfqBarRecord(BaseModel):
    trade_date: date
    open: str
    high: str
    low: str
    close: str
    volume: int
    amount: str


class QfqDataPage(BaseModel):
    dataset: QfqDatasetRecord
    items: list[QfqBarRecord]
    pagination: Pagination


class QfqJobData(BaseModel):
    job_id: UUID
    job_type: str
    status: str


class QfqDataResponse(SuccessEnvelope):
    data: QfqDataPage


class QfqJobResponse(SuccessEnvelope):
    data: QfqJobData


@router.get("/{symbol}", response_model=QfqDataResponse)
async def get_qfq_data(
    symbol: str,
    application: Application,
    _identity: ReadIdentity,
    start: date | None = None,
    end: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    dataset, result = await application.get_data(
        symbol,
        start=start,
        end=end,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data={
            "dataset": asdict(dataset),
            "items": [asdict(item) for item in result.items],
            "pagination": {
                "page": result.page,
                "page_size": result.page_size,
                "total": result.total,
            },
        }
    )


@router.post(
    "/{symbol}/refresh",
    status_code=202,
    response_model=QfqJobResponse,
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
async def refresh_qfq_data(
    symbol: str,
    body: RefreshRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    if not body.confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认刷新前复权数据",
            status_code=422,
        )
    reason = body.reason.strip()
    if not reason:
        raise AppError(
            code="QFQ_WINDOW_INVALID",
            message="刷新原因不能为空",
            status_code=422,
        )
    job = await application.submit_refresh(
        symbol=symbol,
        start=body.start,
        end=body.end,
        as_of_date=body.as_of_date,
        reason=reason,
        idempotency_key=idempotency_key,
        request_id=identity.audit_context.request_id,
        actor_user_id=str(identity.user.id),
        session_id=str(identity.session.id),
        trusted_ip=identity.audit_context.trusted_ip or "unknown",
    )
    return success_response(
        data={
            "job_id": str(job.id),
            "job_type": job.job_type,
            "status": job.status,
        },
        code="JOB_ACCEPTED",
        message="前复权刷新任务已创建",
    )
