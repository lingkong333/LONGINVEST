from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.notifications.admin import (
    notification_delivery_allowed_actions,
)
from long_invest.modules.notifications.application import (
    NotificationAdminApplication,
    get_notification_admin_application,
)
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.template_catalog import GIT_TEMPLATE_REGISTRY
from long_invest.modules.notifications.templates import StrictTemplateRenderer
from long_invest.modules.settings.application import (
    SettingsApplication,
    get_settings_application,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(tags=["notifications"])
Application = Annotated[
    NotificationAdminApplication, Depends(get_notification_admin_application)
]
SettingsApplicationDependency = Annotated[
    SettingsApplication, Depends(get_settings_application)
]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MutationRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=500)
    confirm: StrictBool


class RetryDeliveryRequest(MutationRequest):
    confirm_duplicate_risk: StrictBool = False


class RetryBatchRequest(MutationRequest):
    delivery_ids: list[UUID] = Field(min_length=1, max_length=100)


class TemplatePreviewRequest(StrictRequest):
    template_type: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    variables: dict[str, Any]
    test_message: bool = False


class TemplateTypePreviewRequest(StrictRequest):
    version: str = Field(min_length=1, max_length=100)
    variables: dict[str, Any]
    test_message: bool = False


class TemplateActivateRequest(MutationRequest):
    version: str = Field(min_length=1, max_length=100)


class ChannelTestRequest(MutationRequest):
    message: str = Field(min_length=1, max_length=1000)


class SettingMutationRequest(MutationRequest):
    value: dict[str, Any]
    expected_version: int = Field(ge=1)


class TemplateVersionData(BaseModel):
    template_type: str
    version: str
    active: bool
    source: str
    created_at: datetime


class TemplateListData(BaseModel):
    items: list[TemplateVersionData]


class TemplateListResponse(SuccessEnvelope):
    data: TemplateListData


class TemplateActivationData(BaseModel):
    template_type: str
    version: str
    changed: bool
    replayed: bool


class TemplateActivationResponse(SuccessEnvelope):
    data: TemplateActivationData


class ChannelActionData(BaseModel):
    outcome: str
    code: str
    summary: str
    retryable: bool
    possibly_delivered: bool
    details: dict[str, Any]
    replayed: bool


class ChannelActionResponse(SuccessEnvelope):
    data: ChannelActionData


class CircuitResetData(BaseModel):
    channel: DeliveryChannel
    state: str
    consecutive_failures: int
    cooldown_level: int
    retry_at: datetime | None
    replayed: bool


class CircuitResetResponse(SuccessEnvelope):
    data: CircuitResetData


PolicyScope = Literal["global", "signals", "system-alerts"]
_POLICY_KEYS = {
    "global": "notification.policy.global",
    "signals": "notification.policy.signals",
    "system-alerts": "notification.policy.system_alerts",
}
_CHANNEL_KEYS = {
    DeliveryChannel.WECOM: "notification.channel.wecom",
    DeliveryChannel.EMAIL: "notification.channel.email",
}
_CHANNEL_SECRET_KEYS = {
    DeliveryChannel.WECOM: "notification.wecom.webhook",
    DeliveryChannel.EMAIL: "notification.email.password",
}


@router.get("/api/v1/notification-events", response_model=SuccessEnvelope)
@router.get("/api/v1/notifications/events", response_model=SuccessEnvelope)
async def list_events(
    application: Application,
    _identity: ReadIdentity,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: NotificationEventStatus | None = None,
    event_type: str | None = Query(None, max_length=100),
):
    result = await application.read(
        "list_events",
        page=page,
        page_size=page_size,
        status=status,
        event_type=event_type,
    )
    return success_response(data=_page(result, _event))


@router.get("/api/v1/notifications/events/{event_id}", response_model=SuccessEnvelope)
async def get_event(event_id: UUID, application: Application, _identity: ReadIdentity):
    result = await application.read("get_event_detail", event_id)
    return success_response(
        data={
            "event": _event(result.event),
            "deliveries": [_delivery(item) for item in result.deliveries],
        }
    )


@router.get("/api/v1/notification-events/{id}", response_model=SuccessEnvelope)
async def get_event_by_id(id: UUID, application: Application, identity: ReadIdentity):
    return await get_event(id, application, identity)


@router.get("/api/v1/notification-deliveries", response_model=SuccessEnvelope)
@router.get("/api/v1/notifications/deliveries", response_model=SuccessEnvelope)
async def list_deliveries(
    application: Application,
    _identity: ReadIdentity,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    event_id: UUID | None = None,
    status: NotificationDeliveryStatus | None = None,
    channel: DeliveryChannel | None = None,
):
    result = await application.read(
        "list_deliveries",
        page=page,
        page_size=page_size,
        event_id=event_id,
        status=status,
        channel=channel,
    )
    return success_response(data=_page(result, _delivery))


@router.get(
    "/api/v1/notifications/deliveries/{delivery_id}/attempts",
    response_model=SuccessEnvelope,
)
async def list_attempts(
    delivery_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    result = await application.read(
        "list_attempts", delivery_id, page=page, page_size=page_size
    )
    return success_response(data=_page(result, _attempt))


@router.get(
    "/api/v1/notification-deliveries/{id}/attempts",
    response_model=SuccessEnvelope,
)
async def list_attempts_by_id(
    id: UUID,
    application: Application,
    identity: ReadIdentity,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    return await list_attempts(id, application, identity, page, page_size)


@router.post(
    "/api/v1/notifications/deliveries/{delivery_id}/retry",
    response_model=SuccessEnvelope,
)
async def retry_delivery(
    delivery_id: UUID,
    body: RetryDeliveryRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await application.mutate(
        "retry_delivery",
        delivery_id,
        body.confirm_duplicate_risk,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(
        data={**_delivery(result.delivery), "changed": result.changed}
    )


@router.post(
    "/api/v1/notification-deliveries/{id}/retry",
    response_model=SuccessEnvelope,
)
async def retry_delivery_by_id(
    id: UUID,
    body: RetryDeliveryRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    return await retry_delivery(id, body, application, identity, idempotency_key)


@router.post(
    "/api/v1/notifications/deliveries/{delivery_id}/cancel",
    response_model=SuccessEnvelope,
)
async def cancel_delivery(
    delivery_id: UUID,
    body: MutationRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await application.mutate(
        "cancel_delivery",
        delivery_id,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(
        data={**_delivery(result.delivery), "changed": result.changed}
    )


@router.post(
    "/api/v1/notification-deliveries/{id}/cancel",
    response_model=SuccessEnvelope,
)
async def cancel_delivery_by_id(
    id: UUID,
    body: MutationRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    return await cancel_delivery(id, body, application, identity, idempotency_key)


@router.post(
    "/api/v1/notification-deliveries/retry-batch",
    response_model=SuccessEnvelope,
)
@router.post(
    "/api/v1/notifications/deliveries/retry-batch",
    response_model=SuccessEnvelope,
)
async def retry_batch(
    body: RetryBatchRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await application.mutate(
        "retry_failed_batch",
        body.delivery_ids,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(
        data={
            "retried": [_delivery(item) for item in result.retried],
            "failures": [
                {"delivery_id": item.delivery_id, "code": item.code}
                for item in result.failures
            ],
        }
    )


@router.get("/api/v1/notification-templates", response_model=TemplateListResponse)
@router.get("/api/v1/notifications/templates", response_model=TemplateListResponse)
async def list_templates(application: Application, _identity: ReadIdentity):
    return success_response(data={"items": await application.list_templates()})


@router.get("/api/v1/notification-policies/{scope}", response_model=SuccessEnvelope)
@router.get("/api/v1/notifications/policies/{scope}", response_model=SuccessEnvelope)
async def get_policy(
    scope: PolicyScope,
    settings: SettingsApplicationDependency,
    _identity: ReadIdentity,
):
    return success_response(
        data=await settings.read("get_setting", _POLICY_KEYS[scope])
    )


@router.patch("/api/v1/notification-policies/{scope}", response_model=SuccessEnvelope)
@router.patch("/api/v1/notifications/policies/{scope}", response_model=SuccessEnvelope)
async def update_policy(
    scope: PolicyScope,
    body: SettingMutationRequest,
    settings: SettingsApplicationDependency,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await settings.write(
        "update_setting",
        _POLICY_KEYS[scope],
        value=body.value,
        expected_version=body.expected_version,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(data=result)


@router.get("/api/v1/notifications/channels", response_model=SuccessEnvelope)
async def get_channels(
    settings: SettingsApplicationDependency,
    _identity: ReadIdentity,
):
    configured = await settings.read("list_settings")
    channel_settings = [
        item for item in configured if item["key"] in _CHANNEL_KEYS.values()
    ]
    secrets = await settings.read("secret_statuses")
    return success_response(data={"channels": channel_settings, "secrets": secrets})


@router.get("/api/v1/notification-channels/{channel}", response_model=SuccessEnvelope)
async def get_channel(
    channel: DeliveryChannel,
    settings: SettingsApplicationDependency,
    _identity: ReadIdentity,
):
    configured = await settings.read("get_setting", _CHANNEL_KEYS[channel])
    secret_statuses = await settings.read("secret_statuses")
    secret_status = next(
        (
            item
            for item in secret_statuses
            if item["key"] == _CHANNEL_SECRET_KEYS[channel]
        ),
        None,
    )
    return success_response(
        data={"channel": channel, "setting": configured, "secret": secret_status}
    )


@router.patch("/api/v1/notification-channels/{channel}", response_model=SuccessEnvelope)
@router.patch(
    "/api/v1/notifications/channels/{channel}", response_model=SuccessEnvelope
)
async def update_channel(
    channel: DeliveryChannel,
    body: SettingMutationRequest,
    settings: SettingsApplicationDependency,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await settings.write(
        "update_setting",
        _CHANNEL_KEYS[channel],
        value=body.value,
        expected_version=body.expected_version,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(data=result)


@router.post("/api/v1/notifications/templates/preview", response_model=SuccessEnvelope)
async def preview_template(body: TemplatePreviewRequest, _identity: WriteIdentity):
    return _preview_template(
        body.template_type,
        body.version,
        body.variables,
        test_message=body.test_message,
    )


@router.post(
    "/api/v1/notification-templates/{type}/preview",
    response_model=SuccessEnvelope,
)
async def preview_template_type(
    type: str,
    body: TemplateTypePreviewRequest,
    _identity: WriteIdentity,
):
    return _preview_template(
        type,
        body.version,
        body.variables,
        test_message=body.test_message,
    )


@router.post(
    "/api/v1/notification-templates/{type}/activate",
    response_model=TemplateActivationResponse,
)
async def activate_template(
    type: str,
    body: TemplateActivateRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await application.activate_template(
        type,
        body.version,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(data=result, code="NOTIFICATION_TEMPLATE_ACTIVATED")


def _preview_template(
    template_type: str,
    version: str,
    variables: dict[str, Any],
    *,
    test_message: bool,
):
    try:
        definition = GIT_TEMPLATE_REGISTRY.resolve(template_type, version)
        rendered = StrictTemplateRenderer().render(
            definition, variables, test_message=test_message
        )
    except (LookupError, ValueError) as exc:
        raise AppError(
            code=getattr(exc, "code", "NOTIFICATION_TEMPLATE_INVALID"),
            message=str(exc),
            status_code=422,
        ) from exc
    return success_response(
        data={
            "template_type": rendered.template_type,
            "version": rendered.version,
            "subject": rendered.subject,
            "text": rendered.text,
            "html": rendered.html,
        }
    )


@router.post(
    "/api/v1/notification-channels/{channel}/test",
    response_model=SuccessEnvelope,
)
@router.post(
    "/api/v1/notifications/channels/{channel}/test",
    response_model=SuccessEnvelope,
)
async def test_channel(
    channel: DeliveryChannel,
    body: ChannelTestRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await application.test_channel(
        channel,
        message=body.message,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(data=result)


@router.post(
    "/api/v1/notification-channels/{channel}/probe",
    response_model=ChannelActionResponse,
)
async def probe_channel(
    channel: DeliveryChannel,
    body: ChannelTestRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await application.probe_channel(
        channel,
        message=body.message,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(data=result, code="NOTIFICATION_CHANNEL_PROBED")


@router.post(
    "/api/v1/notification-channels/{channel}/reset-circuit",
    response_model=CircuitResetResponse,
)
async def reset_channel_circuit(
    channel: DeliveryChannel,
    body: MutationRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
):
    _confirm(body.confirm)
    result = await application.reset_circuit(
        channel,
        reason=body.reason,
        idempotency_key=idempotency_key,
        **_context(identity),
    )
    return success_response(data=result, code="NOTIFICATION_CIRCUIT_RESET")


def _page(value, serializer):
    return {
        "items": [serializer(item) for item in value.items],
        "page": value.page,
        "page_size": value.page_size,
        "total": value.total,
    }


def _event(item):
    return {
        "id": item.id,
        "event_type": item.event_type,
        "business_event_type": item.business_event_type,
        "business_event_id": item.business_event_id,
        "business_object_type": item.business_object_type,
        "business_object_id": item.business_object_id,
        "severity": item.severity,
        "status": item.status,
        "eligibility_status": item.eligibility_status,
        "suppression_reason": item.suppression_reason,
        "effective_channels": item.effective_channels,
        "template_version": item.template_version,
        "request_id": item.request_id,
        "created_at": item.created_at,
    }


def _delivery(item):
    status = NotificationDeliveryStatus(item.status)
    return {
        "id": item.id,
        "event_id": item.event_id,
        "generation": item.generation,
        "channel": item.channel,
        "config_version": item.config_version,
        "target_fingerprint": item.target_fingerprint,
        "status": status,
        "allowed_actions": list(notification_delivery_allowed_actions(status)),
        "requires_duplicate_confirmation": status
        in {
            NotificationDeliveryStatus.SENT,
            NotificationDeliveryStatus.OUTCOME_UNKNOWN,
        },
        "attempt_count": item.attempt_count,
        "next_retry_at": item.next_retry_at,
        "sent_at": item.sent_at,
        "error_code": item.error_code,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _attempt(item):
    return {
        "id": item.id,
        "delivery_id": item.delivery_id,
        "attempt_no": item.attempt_no,
        "phase": item.phase,
        "duration_ms": item.duration_ms,
        "outcome": item.outcome,
        "possibly_delivered": item.possibly_delivered,
        "request_id": item.request_id,
        "error_code": item.error_code,
        "response_summary": item.response_summary,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
    }


def _confirm(value: bool) -> None:
    if not value:
        raise AppError(
            code="NOTIFICATION_CONFIRMATION_REQUIRED",
            message="请确认本次通知操作",
            status_code=422,
        )


def _context(identity: AuthenticatedRequest):
    return {
        "request_id": identity.audit_context.request_id,
        "actor_user_id": str(identity.user.id),
        "session_id": str(identity.session.id),
        "trusted_ip": identity.audit_context.trusted_ip or "unknown",
    }
