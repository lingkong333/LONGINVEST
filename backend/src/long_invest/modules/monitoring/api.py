from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.monitoring.application import (
    MonitorSubscriptionApplication,
    get_monitor_subscription_application,
)
from long_invest.modules.monitoring.contracts import (
    SubscriptionAction,
    SubscriptionNotificationChannel,
    SubscriptionNotificationMode,
)
from long_invest.modules.monitoring.service import (
    SubscriptionAuditContext,
    subscription_allowed_actions,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(
    prefix="/api/v1/monitor-subscriptions", tags=["monitor-subscriptions"]
)
Application = Annotated[
    MonitorSubscriptionApplication, Depends(get_monitor_subscription_application)
]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigFields(StrictRequest):
    schedule_id: UUID | None = None
    target_mode: str = "MANUAL"
    target_version_id: UUID | None = None
    strategy_version_id: UUID | None = None
    parameters: dict[str, Any] = {}
    hysteresis_ratio: Decimal = Field(default=Decimal("0"), ge=0)
    hysteresis_min: Decimal = Field(default=Decimal("0"), ge=0)
    notification_mode: SubscriptionNotificationMode = (
        SubscriptionNotificationMode.INHERIT
    )
    notification_channels: tuple[SubscriptionNotificationChannel, ...] = ()


class CreateRequest(ConfigFields):
    symbol: str = Field(min_length=9, max_length=16)
    reason: str = Field(min_length=1, max_length=500, pattern=r".*\S.*")
    confirm: StrictBool


class ConfigureRequest(ConfigFields):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500, pattern=r".*\S.*")
    confirm: StrictBool


class TransitionRequest(StrictRequest):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500, pattern=r".*\S.*")
    confirm: StrictBool


class NotificationPolicyRequest(StrictRequest):
    mode: SubscriptionNotificationMode
    channels: tuple[SubscriptionNotificationChannel, ...] = ()
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500, pattern=r".*\S.*")
    confirm: StrictBool


class SubscriptionRecord(BaseModel):
    id: UUID
    security_id: UUID
    symbol: str
    status: str
    version: int
    current_revision_id: UUID | None
    archived_at: datetime | None
    allowed_actions: list[SubscriptionAction]


class RevisionRecord(BaseModel):
    id: UUID
    subscription_id: UUID
    revision_no: int
    schedule_id: UUID | None
    schedule_revision_id: UUID | None
    target_mode: str
    target_version_id: UUID | None
    strategy_version_id: UUID | None
    parameters: dict[str, Any]
    hysteresis_ratio: Decimal
    hysteresis_min: Decimal
    notification_mode: SubscriptionNotificationMode
    notification_channels: list[SubscriptionNotificationChannel]
    reason: str


class NotificationPolicyData(BaseModel):
    subscription_id: UUID
    subscription_version: int
    revision_id: UUID
    revision_no: int
    mode: SubscriptionNotificationMode
    channels: list[SubscriptionNotificationChannel]


class NotificationPolicyResponse(SuccessEnvelope):
    data: NotificationPolicyData


class NotificationPolicyMutationData(BaseModel):
    policy: NotificationPolicyData
    replayed: bool


class NotificationPolicyMutationResponse(SuccessEnvelope):
    data: NotificationPolicyMutationData


class ResultData(BaseModel):
    subscription: SubscriptionRecord
    revision: RevisionRecord
    replayed: bool


class ResultResponse(SuccessEnvelope):
    data: ResultData


class ActionData(BaseModel):
    subscription_id: UUID
    job_id: UUID
    status: str


class ActionResponse(SuccessEnvelope):
    data: ActionData


class ListData(BaseModel):
    items: list[SubscriptionRecord]


class ListResponse(SuccessEnvelope):
    data: ListData


class DetailData(BaseModel):
    subscription: SubscriptionRecord
    revisions: list[RevisionRecord]


class DetailResponse(SuccessEnvelope):
    data: DetailData


@router.get("", response_model=ListResponse)
async def list_subscriptions(
    application: Application,
    _identity: ReadIdentity,
    include_archived: bool = Query(False),
):
    return success_response(
        data={
            "items": [
                _owner(x)
                for x in await application.list(include_archived=include_archived)
            ]
        }
    )


@router.get("/{subscription_id}", response_model=DetailResponse)
async def get_subscription(
    subscription_id: UUID, application: Application, _identity: ReadIdentity
):
    return success_response(
        data={
            "subscription": _owner(await application.get(subscription_id)),
            "revisions": [
                _revision(x) for x in await application.revisions(subscription_id)
            ],
        }
    )


@router.get(
    "/{subscription_id}/notification-policy",
    response_model=NotificationPolicyResponse,
)
async def get_notification_policy(
    subscription_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    revision_id: UUID | None = None,
):
    policy = await application.notification_policy(
        subscription_id, revision_id=revision_id
    )
    return success_response(data=policy.model_dump(mode="json"))


@router.patch(
    "/{subscription_id}/notification-policy",
    response_model=NotificationPolicyMutationResponse,
)
async def update_notification_policy(
    subscription_id: UUID,
    body: NotificationPolicyRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await application.configure_notification_policy(
        subscription_id,
        mode=body.mode,
        channels=body.channels,
        expected_version=body.expected_version,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    policy = await application.notification_policy(
        subscription_id, revision_id=result.revision.id
    )
    return success_response(
        data={
            "policy": policy.model_dump(mode="json"),
            "replayed": result.replayed,
        },
        code="MONITOR_NOTIFICATION_POLICY_UPDATED",
    )


@router.post("", response_model=ResultResponse)
async def create_subscription(
    body: CreateRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    return success_response(
        data=_result(
            await application.create(
                symbol=body.symbol,
                reason=body.reason,
                idempotency_key=idempotency_key,
                **body.model_dump(exclude={"symbol", "reason", "confirm"}),
                **_context(identity),
            )
        )
    )


@router.patch("/{subscription_id}", response_model=ResultResponse)
async def configure_subscription(
    subscription_id: UUID,
    body: ConfigureRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    return success_response(
        data=_result(
            await application.configure(
                subscription_id,
                reason=body.reason,
                idempotency_key=idempotency_key,
                **body.model_dump(exclude={"reason", "confirm"}),
                **_context(identity),
            )
        )
    )


async def _transition(method, id, body, app, identity, key):
    _confirm(body.confirm)
    return success_response(
        data=_result(
            await getattr(app, method)(
                id,
                expected_version=body.expected_version,
                reason=body.reason,
                idempotency_key=key,
                **_context(identity),
            )
        )
    )


@router.post("/{subscription_id}/enable", response_model=ResultResponse)
async def enable(
    subscription_id: UUID,
    body: TransitionRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    return await _transition(
        "enable", subscription_id, body, application, identity, idempotency_key
    )


@router.post("/{subscription_id}/disable", response_model=ResultResponse)
async def disable(
    subscription_id: UUID,
    body: TransitionRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    return await _transition(
        "pause", subscription_id, body, application, identity, idempotency_key
    )


@router.post("/{subscription_id}/archive", response_model=ResultResponse)
async def archive(
    subscription_id: UUID,
    body: TransitionRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    return await _transition(
        "archive", subscription_id, body, application, identity, idempotency_key
    )


@router.post("/{subscription_id}/restore", response_model=ResultResponse)
async def restore(
    subscription_id: UUID,
    body: TransitionRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    return await _transition(
        "restore", subscription_id, body, application, identity, idempotency_key
    )


@router.post(
    "/{subscription_id}/check-now", response_model=ActionResponse, status_code=202
)
async def check_now(
    subscription_id: UUID,
    body: TransitionRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
):
    _confirm(body.confirm)
    job = await application.check_now(
        subscription_id,
        expected_version=body.expected_version,
        idempotency_key=key.strip(),
        request_id=identity.audit_context.request_id,
        actor_user_id=str(identity.user.id),
        reason=body.reason,
    )
    return success_response(
        data={
            "subscription_id": subscription_id,
            "job_id": job.id,
            "status": str(job.status),
        },
        code="MONITOR_CHECK_REQUESTED",
    )


@router.post(
    "/{subscription_id}/diagnose", response_model=ActionResponse, status_code=202
)
async def diagnose(
    subscription_id: UUID,
    body: TransitionRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
):
    _confirm(body.confirm)
    job = await application.diagnose(
        subscription_id,
        expected_version=body.expected_version,
        idempotency_key=key.strip(),
        request_id=identity.audit_context.request_id,
        actor_user_id=str(identity.user.id),
        session_id=str(identity.session.id),
        trusted_ip=identity.audit_context.trusted_ip or "unknown",
        reason=body.reason,
    )
    return success_response(
        data={
            "subscription_id": subscription_id,
            "job_id": job.id,
            "status": str(job.status),
        },
        code="MONITOR_DIAGNOSTIC_REQUESTED",
    )


def _owner(x):
    owner = {
        k: getattr(x, k)
        for k in (
            "id",
            "security_id",
            "symbol",
            "status",
            "version",
            "current_revision_id",
            "archived_at",
        )
    }
    owner["allowed_actions"] = list(subscription_allowed_actions(x.status))
    return owner


def _revision(x):
    return {
        k: getattr(x, k)
        for k in (
            "id",
            "subscription_id",
            "revision_no",
            "schedule_id",
            "schedule_revision_id",
            "target_mode",
            "target_version_id",
            "strategy_version_id",
            "parameters",
            "hysteresis_ratio",
            "hysteresis_min",
            "notification_mode",
            "notification_channels",
            "reason",
        )
    }


def _result(x):
    return {
        "subscription": _owner(x.subscription),
        "revision": _revision(x.revision),
        "replayed": x.replayed,
    }


def _confirm(v):
    if not v:
        raise AppError(
            code="MONITOR_SUBSCRIPTION_CONFIRMATION_REQUIRED",
            message="请确认订阅操作",
            status_code=422,
        )


def _context(i):
    return {
        "audit_context": SubscriptionAuditContext(
            request_id=i.audit_context.request_id,
            actor_user_id=str(i.user.id),
            session_id=str(i.session.id),
            trusted_ip=i.audit_context.trusted_ip or "unknown",
        )
    }
