from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.providers.contracts import validate_symbol
from long_invest.modules.quotes.application import (
    QuoteApplication,
    get_quote_application,
)
from long_invest.modules.quotes.contracts import QuoteCycleStatus
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(tags=["quotes"])

ApplicationDependency = Annotated[QuoteApplication, Depends(get_quote_application)]
ReadAuth = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteAuth = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=160)
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualQuoteRequest(StrictRequest):
    symbols: tuple[str, ...] = Field(min_length=1, max_length=200)
    timeout_seconds: int = Field(default=30, ge=10, le=60)
    confirm: bool
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_symbols(value)


class DiagnoseQuoteRequest(StrictRequest):
    symbols: tuple[str, ...] = Field(min_length=1, max_length=200)
    confirm: bool
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_symbols(value)


class QuoteCycleRecord(BaseModel):
    id: UUID
    status: QuoteCycleStatus
    expected_count: int
    valid_count: int
    missing_count: int
    conflict_count: int
    failed_count: int
    eligible_item_ids: list[UUID]
    eligible_symbols: list[str]
    scheduled_at: datetime
    started_at: datetime | None
    deadline_at: datetime | None
    finalized_at: datetime | None
    schedule_occurrence_id: UUID | None
    subscription_snapshot_version: int | None


class QuoteCyclePageData(BaseModel):
    items: list[QuoteCycleRecord]
    total: int
    page: int
    page_size: int
    allowed_actions: list[str]


class QuoteItemRecord(BaseModel):
    id: UUID
    cycle_id: UUID
    symbol: str
    status: str
    price: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    previous_close: Decimal | None
    volume: int | None
    amount: Decimal | None
    quote_time: datetime | None
    received_at: datetime | None
    provider: str | None
    error_code: str | None
    conflict_evidence: dict[str, Any] | None
    eligible_for_evaluation: bool
    expected_subscription_version: int | None


class QuoteItemsData(BaseModel):
    items: list[QuoteItemRecord]


class QuoteJobData(BaseModel):
    job_id: UUID
    status: str


class QuoteCyclePageResponse(SuccessEnvelope):
    data: QuoteCyclePageData


class QuoteItemsResponse(SuccessEnvelope):
    data: QuoteItemsData


class QuoteJobResponse(SuccessEnvelope):
    data: QuoteJobData


@router.get(
    "/api/v1/quote-cycles",
    response_model=QuoteCyclePageResponse,
)
async def list_cycles(
    application: ApplicationDependency,
    _authenticated: ReadAuth,
    status_filter: Annotated[QuoteCycleStatus | None, Query(alias="status")] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict:
    result = await application.list_cycles(
        status=status_filter, page=page, page_size=page_size
    )
    actions = await application.allowed_actions()
    return success_response(
        data={
            **_json_data(result),
            "allowed_actions": [action.value for action in actions],
        }
    )


@router.get(
    "/api/v1/quote-cycles/{cycle_id}/items",
    response_model=QuoteItemsResponse,
)
async def list_cycle_items(
    cycle_id: UUID,
    application: ApplicationDependency,
    _authenticated: ReadAuth,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=200),
) -> dict:
    result = await application.list_items(cycle_id, page=page, page_size=page_size)
    return success_response(data={"items": _json_data(result)})


@router.post(
    "/api/v1/quote-cycles/manual",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=QuoteJobResponse,
)
async def submit_manual_cycle(
    body: ManualQuoteRequest,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    job = await application.submit_manual(
        symbols=body.symbols,
        timeout_seconds=body.timeout_seconds,
        idempotency_key=_idempotency_key(idempotency_key),
        request_id=authenticated.audit_context.request_id,
        created_by_user_id=str(authenticated.user.id),
        reason=body.reason,
    )
    return success_response(data=_job_data(job), code="JOB_ACCEPTED")


@router.post(
    "/api/v1/quotes/diagnose",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=QuoteJobResponse,
)
async def diagnose_quotes(
    body: DiagnoseQuoteRequest,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    job = await application.submit_diagnostic(
        symbols=body.symbols,
        idempotency_key=_idempotency_key(idempotency_key),
        request_id=authenticated.audit_context.request_id,
        created_by_user_id=str(authenticated.user.id),
        session_id=str(authenticated.session.id),
        trusted_ip=authenticated.audit_context.trusted_ip or "unknown",
        reason=body.reason,
    )
    return success_response(data=_job_data(job), code="JOB_ACCEPTED")


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认行情任务操作",
            status_code=422,
        )


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="行情写操作必须提供幂等键",
            status_code=422,
        )
    return normalized


def _validate_symbols(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(value)) != len(value):
        raise ValueError("股票范围不能包含重复代码")
    for symbol in value:
        validate_symbol(symbol)
    return value


def _job_data(job: Any) -> dict[str, object]:
    value = job.status.value if hasattr(job.status, "value") else str(job.status)
    return {"job_id": str(job.id), "status": value}


def _json_data(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_data(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_data(item) for item in value]
    return value
