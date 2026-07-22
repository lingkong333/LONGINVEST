from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.settings.application import (
    SettingsApplication,
    get_settings_application,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(prefix="/api/v1", tags=["settings"])
Application = Annotated[SettingsApplication, Depends(get_settings_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingUpdateRequest(StrictRequest):
    value: dict[str, Any]
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    confirm: StrictBool


class SettingRollbackRequest(StrictRequest):
    source_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    confirm: StrictBool


class SecretUpdateRequest(StrictRequest):
    value: str | None = Field(default=None, max_length=4096)
    clear_secret: bool = False
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    confirm: StrictBool


@router.get("/settings", response_model=SuccessEnvelope)
async def list_settings(
    application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data={"items": await application.read("list_settings")})


@router.get("/settings/{key}", response_model=SuccessEnvelope)
async def get_setting(
    key: str, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data=await application.read("get_setting", key))


@router.patch("/settings/{key}", response_model=SuccessEnvelope)
async def update_setting(
    key: str,
    body: SettingUpdateRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body.confirm)
    return success_response(
        data=await application.write(
            "update_setting",
            key,
            value=body.value,
            expected_version=body.expected_version,
            reason=body.reason,
            idempotency_key=idempotency_key,
            **_context(identity),
        )
    )


@router.get("/settings/{key}/history", response_model=SuccessEnvelope)
async def setting_history(
    key: str, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data={"items": await application.read("history", key)})


@router.post("/settings/{key}/rollback", response_model=SuccessEnvelope)
async def rollback_setting(
    key: str,
    body: SettingRollbackRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body.confirm)
    return success_response(
        data=await application.write(
            "rollback_setting",
            key,
            source_version=body.source_version,
            expected_version=body.expected_version,
            reason=body.reason,
            idempotency_key=idempotency_key,
            **_context(identity),
        )
    )


@router.get("/secrets/status", response_model=SuccessEnvelope)
async def secret_status(
    application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data={"items": await application.read("secret_statuses")})


@router.patch("/secrets/{key}", response_model=SuccessEnvelope)
async def update_secret(
    key: str,
    body: SecretUpdateRequest,
    application: Application,
    identity: WriteIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body.confirm)
    return success_response(
        data=await application.write(
            "update_secret",
            key,
            value=body.value,
            clear_secret=body.clear_secret,
            expected_version=body.expected_version,
            reason=body.reason,
            idempotency_key=idempotency_key,
            **_context(identity),
        )
    )


def _confirm(value: bool) -> None:
    if not value:
        raise AppError(
            code="SETTINGS_CONFIRMATION_REQUIRED",
            message="请确认本次配置变更",
            status_code=422,
        )


def _context(identity: AuthenticatedRequest) -> dict[str, str]:
    return {
        "request_id": identity.audit_context.request_id,
        "actor_user_id": str(identity.user.id),
        "session_id": str(identity.session.id),
        "trusted_ip": identity.audit_context.trusted_ip or "unknown",
    }
