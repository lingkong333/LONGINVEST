from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query
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


class MarketSnapshotRequest(StrictRequest):
    timeout_seconds: int = Field(default=60, ge=10, le=60)
    confirm: bool
    reason: str = Field(min_length=1, max_length=500)


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


class RealtimeQuoteRecord(BaseModel):
    symbol: str
    price: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    previous_close: Decimal
    volume: int
    amount: Decimal
    quote_time: datetime
    received_at: datetime
    source: str
    source_identity: dict[str, Any] | None


class RealtimeFailureRecord(BaseModel):
    symbol: str
    code: str


class RealtimeCheckData(BaseModel):
    status: str
    mode: str
    scheduled_at: datetime
    started_at: datetime
    completed_at: datetime
    expected_count: int
    valid_count: int
    failed_count: int
    signal_succeeded: int
    signal_failed: int
    quotes: list[RealtimeQuoteRecord]
    failures: list[RealtimeFailureRecord]


class QuoteCyclePageResponse(SuccessEnvelope):
    data: QuoteCyclePageData


class QuoteItemsResponse(SuccessEnvelope):
    data: QuoteItemsData


class RealtimeCheckResponse(SuccessEnvelope):
    data: RealtimeCheckData


class MarketSnapshotData(BaseModel):
    occurrence_id: UUID
    status: str
    trigger_type: str
    scheduled_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expected_count: int
    fetched_count: int
    failed_count: int
    error_code: str | None


class MarketSnapshotResponse(SuccessEnvelope):
    data: MarketSnapshotData


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
    "/api/v1/quotes/check-now",
    response_model=RealtimeCheckResponse,
)
@router.post(
    "/api/v1/quote-cycles/manual",
    response_model=RealtimeCheckResponse,
    deprecated=True,
)
async def submit_manual_cycle(
    body: ManualQuoteRequest,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    result = await application.submit_manual(
        symbols=body.symbols,
        timeout_seconds=body.timeout_seconds,
        idempotency_key=_idempotency_key(idempotency_key),
        request_id=authenticated.audit_context.request_id,
        created_by_user_id=str(authenticated.user.id),
        reason=body.reason,
    )
    return success_response(data=_realtime_data(result), code="QUOTE_CHECK_COMPLETED")


@router.post(
    "/api/v1/quotes/market-snapshot",
    response_model=MarketSnapshotResponse,
)
async def submit_market_snapshot(
    body: MarketSnapshotRequest,
    background_tasks: BackgroundTasks,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    now = datetime.now().astimezone()
    result, symbols, definition_key = await application.start_market_snapshot(
        scheduled_at=now,
        trigger_type="MANUAL",
        idempotency_key=_idempotency_key(idempotency_key),
        reason=body.reason,
        created_by_user_id=str(authenticated.user.id),
    )
    if result.status == "RUNNING":
        background_tasks.add_task(
            application.complete_market_snapshot,
            occurrence_id=result.id,
            symbols=symbols,
            definition_key=definition_key,
            scheduled_at=now,
            trigger_type="MANUAL",
            timeout_seconds=body.timeout_seconds,
        )
    return success_response(
        data={
            "occurrence_id": result.id,
            "status": result.status,
            "trigger_type": result.trigger_type,
            "scheduled_at": result.scheduled_at,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "expected_count": result.expected_count,
            "fetched_count": result.fetched_count,
            "failed_count": result.failed_count,
            "error_code": result.error_code,
        },
        code="QUOTE_MARKET_SNAPSHOT_STARTED",
    )


@router.post(
    "/api/v1/quotes/diagnose",
    response_model=RealtimeCheckResponse,
)
async def diagnose_quotes(
    body: DiagnoseQuoteRequest,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    _require_confirmation(body.confirm)
    result = await application.submit_diagnostic(
        symbols=body.symbols,
        idempotency_key=_idempotency_key(idempotency_key),
        request_id=authenticated.audit_context.request_id,
        created_by_user_id=str(authenticated.user.id),
        session_id=str(authenticated.session.id),
        trusted_ip=authenticated.audit_context.trusted_ip or "unknown",
        reason=body.reason,
    )
    return success_response(
        data=_realtime_data(result), code="QUOTE_DIAGNOSTIC_COMPLETED"
    )


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


def _realtime_data(result: Any) -> dict[str, object]:
    return {
        "status": result.status,
        "mode": result.mode,
        "scheduled_at": result.scheduled_at,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "expected_count": result.expected_count,
        "valid_count": result.valid_count,
        "failed_count": result.failed_count,
        "signal_succeeded": result.signal_succeeded,
        "signal_failed": result.signal_failed,
        "quotes": [
            {
                "symbol": item.symbol,
                "price": item.price,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "previous_close": item.previous_close,
                "volume": item.volume,
                "amount": item.amount,
                "quote_time": item.quote_time,
                "received_at": item.received_at,
                "source": item.source,
                "source_identity": _json_data(item.source_identity),
            }
            for item in result.quotes
        ],
        "failures": [
            {"symbol": item.symbol, "code": item.code}
            for item in result.failures
        ],
    }


def _json_data(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_data(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_data(item) for item in value]
    return value
