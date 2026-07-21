from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.signals.application import SignalApplication
from long_invest.modules.signals.contracts import (
    SignalEvaluationView,
    SignalEventView,
    SignalReevaluationCommand,
    SignalReevaluationResult,
    SignalStateMutationResult,
    SignalStateResetCommand,
    SignalStateView,
)
from long_invest.platform.database.engine import get_database
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(tags=["signals"])


def get_signal_application() -> SignalApplication:
    return SignalApplication(get_database())


Application = Annotated[SignalApplication, Depends(get_signal_application)]
ReadIdentity = Annotated[
    AuthenticatedRequest,
    Depends(require_authenticated_request),
]
WriteIdentity = Annotated[
    AuthenticatedRequest,
    Depends(require_verified_write_request),
]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200),
]


class SignalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=1)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SignalStatePageData(BaseModel):
    items: list[SignalStateView]
    pagination: Pagination


class SignalEventPageData(BaseModel):
    items: list[SignalEventView]
    pagination: Pagination


class SignalEvaluationPageData(BaseModel):
    items: list[SignalEvaluationView]
    pagination: Pagination


class SignalStatePageResponse(SuccessEnvelope):
    data: SignalStatePageData


class SignalStateResponse(SuccessEnvelope):
    data: SignalStateView


class SignalEventPageResponse(SuccessEnvelope):
    data: SignalEventPageData


class SignalEventResponse(SuccessEnvelope):
    data: SignalEventView


class SignalEvaluationPageResponse(SuccessEnvelope):
    data: SignalEvaluationPageData


class SignalEvaluationResponse(SuccessEnvelope):
    data: SignalEvaluationView


class SignalStateMutationResponse(SuccessEnvelope):
    data: SignalStateMutationResult


class SignalReevaluationResponse(SuccessEnvelope):
    data: SignalReevaluationResult


@router.get(
    "/api/v1/signals/states",
    response_model=SignalStatePageResponse,
)
async def list_signal_states(
    _identity: ReadIdentity,
    application: Application,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_states(page=page, page_size=page_size)
    return _page_response(items, total=total, page=page, page_size=page_size)


@router.get(
    "/api/v1/signals/states/{subscription_id}",
    response_model=SignalStateResponse,
)
async def get_signal_state(
    subscription_id: UUID,
    _identity: ReadIdentity,
    application: Application,
) -> dict[str, Any]:
    state = await application.get_state(subscription_id)
    if state is None:
        raise AppError(
            code="SIGNAL_STATE_NOT_FOUND",
            message="订阅尚无信号状态",
            status_code=404,
        )
    return success_response(data=state.model_dump(mode="json"))


@router.get(
    "/api/v1/signal-events",
    response_model=SignalEventPageResponse,
)
async def list_signal_events(
    _identity: ReadIdentity,
    application: Application,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_events(page=page, page_size=page_size)
    return _page_response(items, total=total, page=page, page_size=page_size)


@router.get(
    "/api/v1/signal-events/{event_id}",
    response_model=SignalEventResponse,
)
async def get_signal_event(
    event_id: UUID,
    _identity: ReadIdentity,
    application: Application,
) -> dict[str, Any]:
    event = await application.get_event(event_id)
    if event is None:
        raise AppError(
            code="SIGNAL_EVENT_NOT_FOUND",
            message="信号事件不存在",
            status_code=404,
        )
    return success_response(data=event.model_dump(mode="json"))


@router.get(
    "/api/v1/signal-evaluations",
    response_model=SignalEvaluationPageResponse,
)
async def list_signal_evaluations(
    _identity: ReadIdentity,
    application: Application,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_evaluations(page=page, page_size=page_size)
    return _page_response(items, total=total, page=page, page_size=page_size)


@router.get(
    "/api/v1/signal-evaluations/{evaluation_id}",
    response_model=SignalEvaluationResponse,
)
async def get_signal_evaluation(
    evaluation_id: UUID,
    _identity: ReadIdentity,
    application: Application,
) -> dict[str, Any]:
    evaluation = await application.get_evaluation(evaluation_id)
    if evaluation is None:
        raise AppError(
            code="SIGNAL_EVALUATION_NOT_FOUND",
            message="信号判断记录不存在",
            status_code=404,
        )
    return success_response(data=evaluation.model_dump(mode="json"))


@router.post(
    "/api/v1/signals/states/{subscription_id}/reset",
    response_model=SignalStateMutationResponse,
)
async def reset_signal_state(
    subscription_id: UUID,
    body: SignalActionRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    result = await application.reset(
        SignalStateResetCommand(
            subscription_id=subscription_id,
            reason=body.reason,
            expected_version=body.expected_version,
            idempotency_key=_validated_idempotency_key(idempotency_key),
            **_identity(identity),
        )
    )
    return success_response(data=result.model_dump(mode="json"), code=result.code)


@router.post(
    "/api/v1/signals/states/{subscription_id}/reevaluate",
    response_model=SignalReevaluationResponse,
)
async def reevaluate_signal_state(
    subscription_id: UUID,
    body: SignalActionRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    result = await application.reevaluate(
        SignalReevaluationCommand(
            subscription_id=subscription_id,
            reason=body.reason,
            expected_version=body.expected_version,
            idempotency_key=_validated_idempotency_key(idempotency_key),
            **_identity(identity),
        )
    )
    return success_response(data=result.model_dump(mode="json"), code=result.code)


def _page_response(
    items: tuple[BaseModel, ...],
    *,
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    return success_response(
        data={
            "items": [item.model_dump(mode="json") for item in items],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
            },
        }
    )


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="SIGNAL_CONFIRMATION_REQUIRED",
            message="信号操作需要明确确认",
            status_code=409,
        )


def _validated_idempotency_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="信号写操作需要幂等键",
            status_code=422,
        )
    return key


def _identity(identity: AuthenticatedRequest) -> dict[str, str]:
    return {
        "request_id": identity.audit_context.request_id,
        "actor_user_id": str(identity.user.id),
        "session_id": str(identity.session.id),
        "trusted_ip": identity.audit_context.trusted_ip or "unknown",
    }
