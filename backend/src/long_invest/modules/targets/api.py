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
from long_invest.modules.targets.application import TargetApplication
from long_invest.modules.targets.contracts import (
    ManualTargetCommand,
    RestoreTargetCommand,
    TargetMutationResult,
    TargetRevisionView,
    TargetSnapshot,
    TargetValues,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(tags=["targets"])


def get_target_application() -> TargetApplication:
    # Task 6 replaces this dependency with the transaction-bound monitoring port.
    raise _capability_not_ready()


Application = Annotated[TargetApplication, Depends(get_target_application)]
ReadIdentity = Annotated[
    AuthenticatedRequest, Depends(require_authenticated_request)
]
WriteIdentity = Annotated[
    AuthenticatedRequest, Depends(require_verified_write_request)
]
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

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TargetPageData(BaseModel):
    items: list[TargetSnapshot]
    pagination: Pagination


class TargetHistoryData(BaseModel):
    items: list[TargetRevisionView]


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
    application: Application,
    _identity: ReadIdentity,
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
    application: Application,
    _identity: ReadIdentity,
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
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    items = await application.history(subscription_id)
    return success_response(
        data={"items": [item.model_dump(mode="json") for item in items]}
    )


@router.post(
    "/api/v1/targets/{subscription_id}/manual",
    response_model=TargetMutationResponse,
)
async def set_manual_target(
    subscription_id: UUID,
    body: ManualTargetRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
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
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
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
    body: CapabilityWriteRequest,
    _application: Application,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body)


@router.post(
    "/api/v1/targets/{subscription_id}/retry",
    response_model=CapabilityResponse,
)
async def retry_target(
    subscription_id: UUID,
    body: CapabilityWriteRequest,
    _application: Application,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body)


@router.post(
    "/api/v1/targets/calculate-batch",
    response_model=CapabilityResponse,
)
async def calculate_targets_batch(
    body: CapabilityWriteRequest,
    _application: Application,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body)


@router.get(
    "/api/v1/target-calculation-runs",
    response_model=CapabilityPageResponse,
)
async def list_target_calculation_runs(
    _application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    raise _capability_not_ready()


@router.get(
    "/api/v1/target-reviews",
    response_model=CapabilityPageResponse,
)
async def list_target_reviews(
    _application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    raise _capability_not_ready()


@router.post(
    "/api/v1/target-reviews/{review_id}/approve",
    response_model=CapabilityResponse,
)
async def approve_target_review(
    review_id: UUID,
    body: CapabilityWriteRequest,
    _application: Application,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body)


@router.post(
    "/api/v1/target-reviews/{review_id}/reject",
    response_model=CapabilityResponse,
)
async def reject_target_review(
    review_id: UUID,
    body: CapabilityWriteRequest,
    _application: Application,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body)


@router.post(
    "/api/v1/target-reviews/{review_id}/recalculate",
    response_model=CapabilityResponse,
)
async def recalculate_target_review(
    review_id: UUID,
    body: CapabilityWriteRequest,
    _application: Application,
    _identity_value: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return _unavailable_write(body)


def _unavailable_write(body: CapabilityWriteRequest) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    raise _capability_not_ready()


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="TARGET_CONFIRMATION_REQUIRED",
            message="目标操作需要明确确认",
            status_code=409,
        )


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
