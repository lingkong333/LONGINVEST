from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.targets.application import TargetApplication
from long_invest.modules.targets.contracts import (
    ManualTargetCommand,
    RestoreTargetCommand,
    TargetCalculationRunView,
    TargetMutationResult,
    TargetReviewDetail,
    TargetRevisionView,
    TargetSnapshot,
    TargetValues,
)
from long_invest.modules.targets.strategy_service import (
    CalculateTargetCommand,
    RecalculateReviewCommand,
    RetryTargetCommand,
    ReviewCommand,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(tags=["targets"])


def get_target_application() -> TargetApplication:
    from long_invest.bootstrap.stage4_runtime import build_target_application

    return build_target_application()


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


class BatchCalculateTargetItem(StrictRequest):
    subscription_id: UUID
    target_date: date
    training_start_date: date
    training_end_date: date
    expected_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_training_range(self) -> BatchCalculateTargetItem:
        if self.training_start_date > self.training_end_date:
            raise ValueError("training start date must not be after end date")
        return self


class BatchCalculateTargetRequest(StrictRequest):
    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=500)
    items: list[BatchCalculateTargetItem] = Field(min_length=1, max_length=200)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_unique_subscriptions(self) -> BatchCalculateTargetRequest:
        subscription_ids = [item.subscription_id for item in self.items]
        if len(subscription_ids) != len(set(subscription_ids)):
            raise ValueError("batch subscriptions must be unique")
        return self


class ReviewTargetRequest(CapabilityWriteRequest):
    comment: str = Field(min_length=1, max_length=500)

    @field_validator("comment", mode="before")
    @classmethod
    def strip_comment(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TargetRecord(TargetSnapshot):
    allowed_actions: tuple[str, ...]


class TargetPageData(BaseModel):
    items: list[TargetRecord]
    pagination: Pagination


class TargetHistoryData(BaseModel):
    items: list[TargetRevisionView]
    pagination: Pagination


class TargetPageResponse(SuccessEnvelope):
    data: TargetPageData


class TargetResponse(SuccessEnvelope):
    data: TargetRecord


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
    items: list[TargetReviewDetail]
    pagination: Pagination


class ReviewPageResponse(SuccessEnvelope):
    data: ReviewPageData


class CapabilityResult(BaseModel):
    code: str
    accepted: bool
    run_id: UUID | None = None
    job_id: UUID | None = None
    replayed: bool = False


class CapabilityResponse(SuccessEnvelope):
    data: CapabilityResult


class BatchCapabilityItem(BaseModel):
    subscription_id: UUID
    code: str
    accepted: bool
    run_id: UUID | None = None
    job_id: UUID | None = None
    replayed: bool = False


class BatchCapabilityResult(BaseModel):
    requested: int
    accepted: int
    failed: int
    items: list[BatchCapabilityItem]


class BatchCapabilityResponse(SuccessEnvelope):
    data: BatchCapabilityResult


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
            "items": [_target_record(item) for item in items],
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
    return success_response(data=_target_record(target))


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
    status_code=202,
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
        data={
            "code": result.code,
            "accepted": True,
            "run_id": str(result.run_id),
            "job_id": str(result.job_id),
            "replayed": result.replayed,
        },
        code=result.code,
    )


@router.post(
    "/api/v1/targets/{subscription_id}/retry",
    response_model=CapabilityResponse,
    status_code=202,
)
async def retry_target(
    subscription_id: UUID,
    body: CapabilityWriteRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    result = await application.retry(
        RetryTargetCommand(
            subscription_id=subscription_id,
            reason=body.reason,
            expected_version=body.expected_version,
            idempotency_key=_validated_idempotency_key(idempotency_key),
            **_identity(identity),
        )
    )
    return success_response(
        data={
            "code": result.code,
            "accepted": True,
            "run_id": str(result.run_id),
            "job_id": str(result.job_id),
            "replayed": result.replayed,
        },
        code=result.code,
    )


@router.post(
    "/api/v1/targets/calculate-batch",
    response_model=BatchCapabilityResponse,
    status_code=202,
)
async def calculate_targets_batch(
    body: BatchCalculateTargetRequest,
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    batch_key = _validated_idempotency_key(idempotency_key)
    identity_fields = _identity(identity)
    commands = tuple(
        CalculateTargetCommand(
            subscription_id=item.subscription_id,
            target_date=item.target_date,
            training_start_date=item.training_start_date,
            training_end_date=item.training_end_date,
            reason=body.reason,
            expected_version=item.expected_version,
            idempotency_key=_batch_item_key(batch_key, item.subscription_id),
            **identity_fields,
        )
        for item in body.items
    )
    results = await application.calculate_batch(commands)
    items = [
        {
            "subscription_id": str(result.subscription_id),
            "code": result.code,
            "accepted": result.submission is not None,
            "run_id": (
                str(result.submission.run_id) if result.submission is not None else None
            ),
            "job_id": (
                str(result.submission.job_id) if result.submission is not None else None
            ),
            "replayed": (
                result.submission.replayed if result.submission is not None else False
            ),
        }
        for result in results
    ]
    accepted = sum(item["accepted"] for item in items)
    code = "TARGET_BATCH_ACCEPTED" if accepted == len(items) else "TARGET_BATCH_PARTIAL"
    return success_response(
        data={
            "requested": len(items),
            "accepted": accepted,
            "failed": len(items) - accepted,
            "items": items,
        },
        code=code,
    )


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
    identity: WriteIdentity,
    application: Application,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    result = await application.recalculate_review(
        RecalculateReviewCommand(
            review_id=review_id,
            reason=body.reason,
            expected_version=body.expected_version,
            idempotency_key=_validated_idempotency_key(idempotency_key),
            **_identity(identity),
        )
    )
    return success_response(
        data={
            "code": result.code,
            "accepted": True,
            "run_id": str(result.run_id),
            "job_id": str(result.job_id),
            "replayed": result.replayed,
        },
        code=result.code,
    )


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


def _batch_item_key(batch_key: str, subscription_id: UUID) -> str:
    digest = hashlib.sha256(f"{batch_key}:{subscription_id}".encode()).hexdigest()
    return f"batch:{digest}"


def _target_record(target: TargetSnapshot) -> dict[str, Any]:
    actions = ["MANUAL_EDIT", "RESTORE"]
    if target.status not in {
        "CALCULATING",
        "REVIEW_REQUIRED",
        "ACTIVATING",
    }:
        actions.append("CALCULATE")
    if target.status in {"FAILED", "STALE"}:
        actions.append("RETRY")
    return {
        **target.model_dump(mode="json"),
        "allowed_actions": actions,
    }


def _identity(identity: AuthenticatedRequest) -> dict[str, str]:
    return {
        "request_id": identity.audit_context.request_id,
        "actor_user_id": str(identity.user.id),
        "session_id": str(identity.session.id),
        "trusted_ip": identity.audit_context.trusted_ip or "unknown",
    }
