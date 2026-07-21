from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.monitoring.application import (
    transactional_monitor_subscription_port,
)
from long_invest.modules.strategies.application import get_strategy_application
from long_invest.modules.targets.application import TargetApplication
from long_invest.modules.targets.contracts import (
    ManualTargetCommand,
    RestoreTargetCommand,
    TargetCalculationRunView,
    TargetMutationResult,
    TargetReviewView,
    TargetRevisionView,
    TargetSnapshot,
    TargetValues,
)
from long_invest.modules.targets.strategy_service import (
    CalculateTargetCommand,
    ReviewCommand,
)
from long_invest.platform.database.engine import get_database
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(tags=["targets"])


def get_target_application() -> TargetApplication:
    strategies = get_strategy_application()
    return TargetApplication(
        get_database(),
        subscription_factory=lambda session: transactional_monitor_subscription_port(
            session,
            strategy_readiness=strategies,
            strategy_snapshots=strategies,
        ),
        strategy_application=strategies,
    )


Application = Annotated[TargetApplication, Depends(get_target_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200),
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualTargetRequest(StrictRequest):
    confirm: StrictBool
    target_date: date
    values: TargetValues
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=1)
    large_change_confirmed: StrictBool = False
    switch_to_manual_confirmed: StrictBool = False

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RestoreTargetRequest(StrictRequest):
    confirm: StrictBool
    source_revision_id: UUID
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=1)
    switch_to_manual_confirmed: StrictBool = False

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class CapabilityWriteRequest(StrictRequest):
    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=1)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class CalculateTargetRequest(CapabilityWriteRequest):
    target_date: date
    training_start_date: date
    training_end_date: date


class ReviewTargetRequest(CapabilityWriteRequest):
    comment: str = Field(min_length=1, max_length=500)

    @field_validator("comment", mode="before")
    @classmethod
    def strip_comment(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TargetPageData(BaseModel):
    items: list[TargetSnapshot]
    pagination: Pagination


class TargetHistoryData(BaseModel):
    items: list[TargetRevisionView]
    pagination: Pagination


class TargetPageResponse(SuccessEnvelope):
    data: TargetPageData


class TargetResponse(SuccessEnvelope):
    data: TargetSnapshot


class TargetHistoryResponse(SuccessEnvelope):
    data: TargetHistoryData


class TargetMutationResponse(SuccessEnvelope):
    data: TargetMutationResult


class CapabilityRecord(BaseModel):
    id: UUID
    status: str
    created_at: datetime


class CapabilityPageData(BaseModel):
    items: list[CapabilityRecord]
    pagination: Pagination


class CapabilityPageResponse(SuccessEnvelope):
    data: CapabilityPageData


class CalculationRunPageData(BaseModel):
    items: list[TargetCalculationRunView]
    pagination: Pagination


class CalculationRunPageResponse(SuccessEnvelope):
    data: CalculationRunPageData


class ReviewPageData(BaseModel):
    items: list[TargetReviewView]
    pagination: Pagination


class ReviewPageResponse(SuccessEnvelope):
    data: ReviewPageData


class CapabilityResult(BaseModel):
    code: str
    accepted: bool


class CapabilityResponse(SuccessEnvelope):
    data: CapabilityResult


@router.get(
    "/api/v1/targets",
    response_model=TargetPageResponse,
)
async def list_targets(
    _identity: ReadIdentity,
    application: Application,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list(page=page, page_size=page_size)
    return success_response(
        data={
            "items": [item.model_dump(mode="json") for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.get(
    "/api/v1/targets/{subscription_id}",
    response_model=TargetResponse,
)
async def get_target(
    subscription_id: UUID,
    _identity: ReadIdentity,
    application: Application,
) -> dict[str, Any]:
    target = await application.get(subscription_id)
    if target is None:
        raise AppError(
            code="TARGET_REVISION_NOT_FOUND",
            message="订阅尚无当前目标",
            status_code=404,
        )
    return success_response(data=target.model_dump(mode="json"))


@router.get(
    "/api/v1/targets/{subscription_id}/history",
    response_model=TargetHistoryResponse,
)
async def target_history(
    subscription_id: UUID,
    _identity: ReadIdentity,
    application: Application,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.history(
        subscription_id, page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [item.model_dump(mode="json") for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.post(
    "/api/v1/targets/{subscription_id}/manual",
    response_model=TargetMutationResponse,
)
async def set_manual_target(
    subscription_id: UUID,
    body: ManualTargetRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    idempotency_key = _validated_idempotency_key(idempotency_key)
    result = await application.set_manual(
        ManualTargetCommand(
            subscription_id=subscription_id,
            target_date=body.target_date,
            values=body.values,
            reason=body.reason,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            large_change_confirmed=body.large_change_confirmed,
            switch_to_manual_confirmed=body.switch_to_manual_confirmed,
            **_identity(identity),
        )
    )
    return success_response(data=result.model_dump(mode="json"), code=result.code)


@router.post(
    "/api/v1/targets/{subscription_id}/restore",
    response_model=TargetMutationResponse,
)
async def restore_target(
    subscription_id: UUID,
    body: RestoreTargetRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    idempotency_key = _validated_idempotency_key(idempotency_key)
    result = await application.restore(
        RestoreTargetCommand(
            subscription_id=subscription_id,
            source_revision_id=body.source_revision_id,
            reason=body.reason,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            switch_to_manual_confirmed=body.switch_to_manual_confirmed,
            **_identity(identity),
        )
    )
    return success_response(data=result.model_dump(mode="json"), code=result.code)


@router.post(
    "/api/v1/targets/{subscription_id}/calculate",
    response_model=CapabilityResponse,
)
async def calculate_target(
    subscription_id: UUID,
    body: CalculateTargetRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    result = await application.calculate(
        CalculateTargetCommand(
            subscription_id=subscription_id,
            target_date=body.target_date,
            training_start_date=body.training_start_date,
            training_end_date=body.training_end_date,
            reason=body.reason,
            expected_version=body.expected_version,
            idempotency_key=_validated_idempotency_key(idempotency_key),
            **_identity(identity),
        )
    )
    return success_response(
        data={"code": result.code, "accepted": True}, code=result.code
    )


@router.post(
    "/api/v1/targets/{subscription_id}/retry",
    response_model=CapabilityResponse,
)
async def retry_target(
    subscription_id: UUID,
    body: CapabilityWriteRequest,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body, idempotency_key)


@router.post(
    "/api/v1/targets/calculate-batch",
    response_model=CapabilityResponse,
)
async def calculate_targets_batch(
    body: CapabilityWriteRequest,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body, idempotency_key)


@router.get(
    "/api/v1/target-calculation-runs",
    response_model=CalculationRunPageResponse,
)
async def list_target_calculation_runs(
    _identity: ReadIdentity,
    application: Application,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_calculation_runs(
        page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [item.model_dump(mode="json") for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.get(
    "/api/v1/target-reviews",
    response_model=ReviewPageResponse,
)
async def list_target_reviews(
    _identity: ReadIdentity,
    application: Application,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list_reviews(page=page, page_size=page_size)
    return success_response(
        data={
            "items": [item.model_dump(mode="json") for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.post(
    "/api/v1/target-reviews/{review_id}/approve",
    response_model=CapabilityResponse,
)
async def approve_target_review(
    review_id: UUID,
    body: ReviewTargetRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return await _decide_review(
        review_id, body, identity, application, idempotency_key, approve=True
    )


@router.post(
    "/api/v1/target-reviews/{review_id}/reject",
    response_model=CapabilityResponse,
)
async def reject_target_review(
    review_id: UUID,
    body: ReviewTargetRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return await _decide_review(
        review_id, body, identity, application, idempotency_key, approve=False
    )


@router.post(
    "/api/v1/target-reviews/{review_id}/recalculate",
    response_model=CapabilityResponse,
)
async def recalculate_target_review(
    review_id: UUID,
    body: CapabilityWriteRequest,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body, idempotency_key)


def _unavailable_write(
    body: CapabilityWriteRequest, idempotency_key: str
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    idempotency_key = _validated_idempotency_key(idempotency_key)
    raise _capability_not_ready()


async def _decide_review(
    review_id,
    body,
    identity,
    application,
    idempotency_key,
    *,
    approve,
):
    _require_confirmation(body.confirm)
    _validated_idempotency_key(idempotency_key)
    result = await application.decide_review(
        ReviewCommand(
            review_id=review_id,
            comment=body.comment,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            **_identity(identity),
        ),
        approve=approve,
    )
    return success_response(
        data={"code": result.code, "accepted": True}, code=result.code
    )


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="TARGET_CONFIRMATION_REQUIRED",
            message="目标操作需要明确确认",
            status_code=409,
        )


def _validated_idempotency_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="目标写操作需要幂等键",
            status_code=422,
        )
    return key


def _capability_not_ready() -> AppError:
    return AppError(
        code="TARGET_CAPABILITY_NOT_READY",
        message="目标策略计算与复核能力尚未开放",
        status_code=409,
    )


def _identity(identity: AuthenticatedRequest) -> dict[str, str]:
    return {
        "request_id": identity.audit_context.request_id,
        "actor_user_id": str(identity.user.id),
        "session_id": str(identity.session.id),
        "trusted_ip": identity.audit_context.trusted_ip or "unknown",
    }
