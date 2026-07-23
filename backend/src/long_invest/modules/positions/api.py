from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.positions.application import (
    PositionApplication,
    get_position_application,
)
from long_invest.modules.positions.contracts import (
    PositionAction,
    PositionBatchResult,
    PositionResult,
    PositionStatus,
    PositionView,
)
from long_invest.modules.positions.service import position_allowed_actions
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(tags=["positions"])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PositionChangeRequest(StrictRequest):
    note: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    source: str = Field(default="manual", min_length=1, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("note", "reason", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return _strip_text(value)


class BatchPositionItem(StrictRequest):
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    target: PositionStatus
    note: str | None = Field(default=None, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("note", mode="before")
    @classmethod
    def strip_note(cls, value: object) -> object:
        return _strip_text(value)


class BatchPositionRequest(StrictRequest):
    items: tuple[BatchPositionItem, ...] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    source: str = Field(default="manual", min_length=1, max_length=64)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return _strip_text(value)


class PositionRecord(BaseModel):
    security_id: UUID
    symbol: str
    status: PositionStatus
    version: int
    source: str | None
    updated_at: datetime | None
    allowed_actions: list[PositionAction]


class PositionItems(BaseModel):
    items: list[PositionRecord]
    pagination: Pagination


class PositionHistoryRecord(BaseModel):
    id: UUID
    security_id: UUID
    symbol: str
    before_status: PositionStatus | None
    after_status: PositionStatus
    version: int
    note: str | None
    source: str
    request_id: str
    effective_at: datetime


class PositionHistoryItems(BaseModel):
    items: list[PositionHistoryRecord]
    pagination: Pagination


class PositionResultRecord(BaseModel):
    code: str
    position: PositionRecord
    replayed: bool


class PositionBatchItemRecord(BaseModel):
    symbol: str
    status: str
    code: str
    position: PositionRecord | None


class PositionBatchItems(BaseModel):
    items: list[PositionBatchItemRecord]


class PositionListResponse(SuccessEnvelope):
    data: PositionItems


class PositionResponse(SuccessEnvelope):
    data: PositionRecord


class PositionHistoryResponse(SuccessEnvelope):
    data: PositionHistoryItems


class PositionChangeResponse(SuccessEnvelope):
    data: PositionResultRecord


class PositionBatchResponse(SuccessEnvelope):
    data: PositionBatchItems


Application = Annotated[PositionApplication, Depends(get_position_application)]
ReadAuth = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteAuth = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]


@router.get("/api/v1/positions", response_model=PositionListResponse)
async def list_positions(
    application: Application,
    _authenticated: ReadAuth,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_page(page=page, page_size=page_size)
    return success_response(
        data={
            "items": [_position_data(item) for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.get("/api/v1/position-history", response_model=PositionHistoryResponse)
async def all_position_history(
    application: Application,
    _authenticated: ReadAuth,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.history_page(
        page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [_history_data(item) for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.post("/api/v1/positions/batch", response_model=PositionBatchResponse)
async def batch_positions(
    body: BatchPositionRequest,
    application: Application,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    results = await application.batch_set(
        items=tuple(
            (item.symbol, item.target, item.note, item.expected_version)
            for item in body.items
        ),
        source=body.source,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_identity(authenticated),
    )
    return success_response(
        data={"items": [_batch_result_data(item) for item in results]}
    )


@router.get("/api/v1/positions/{symbol}", response_model=PositionResponse)
async def get_position(
    symbol: str, application: Application, _authenticated: ReadAuth
) -> dict[str, Any]:
    return success_response(
        data=_position_data(await application.get(symbol))
    )


@router.get(
    "/api/v1/positions/{symbol}/history",
    response_model=PositionHistoryResponse,
)
async def position_history(
    symbol: str,
    application: Application,
    _authenticated: ReadAuth,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.history_page(
        symbol, page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [_history_data(item) for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.post(
    "/api/v1/positions/{symbol}/hold",
    response_model=PositionChangeResponse,
)
async def hold_position(
    symbol: str,
    body: PositionChangeRequest,
    application: Application,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    result = await application.set_status(
        symbol=symbol,
        target=PositionStatus.HOLDING,
        note=body.note,
        reason=body.reason,
        source=body.source,
        expected_version=body.expected_version,
        idempotency_key=idempotency_key,
        **_identity(authenticated),
    )
    return success_response(data=_result_data(result))


@router.post(
    "/api/v1/positions/{symbol}/clear",
    response_model=PositionChangeResponse,
)
async def clear_position(
    symbol: str,
    body: PositionChangeRequest,
    application: Application,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    result = await application.set_status(
        symbol=symbol,
        target=PositionStatus.NOT_HOLDING,
        note=body.note,
        reason=body.reason,
        source=body.source,
        expected_version=body.expected_version,
        idempotency_key=idempotency_key,
        **_identity(authenticated),
    )
    return success_response(data=_result_data(result))


def _position_data(item: PositionView) -> dict[str, Any]:
    return {
        **item.model_dump(mode="json"),
        "allowed_actions": list(position_allowed_actions(item.status)),
    }


def _result_data(result: PositionResult) -> dict[str, Any]:
    return {
        "code": result.code,
        "position": _position_data(result.position),
        "replayed": result.replayed,
    }


def _batch_result_data(result: PositionBatchResult) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "status": result.status,
        "code": result.code,
        "position": (
            _position_data(result.position) if result.position is not None else None
        ),
    }


def _identity(authenticated: AuthenticatedRequest) -> dict:
    return {
        "request_id": authenticated.audit_context.request_id,
        "actor_user_id": str(authenticated.user.id),
        "session_id": str(authenticated.session.id),
        "trusted_ip": authenticated.audit_context.trusted_ip or "unknown",
    }


def _history_data(item) -> dict:
    return {
        "id": str(item.id),
        "security_id": str(item.security_id),
        "symbol": item.symbol,
        "before_status": item.before_status,
        "after_status": item.after_status,
        "version": item.position_version,
        "note": item.note,
        "source": item.source,
        "request_id": item.request_id,
        "effective_at": item.effective_at,
    }


def _strip_text(value: object) -> object:
    return value.strip() if isinstance(value, str) else value
