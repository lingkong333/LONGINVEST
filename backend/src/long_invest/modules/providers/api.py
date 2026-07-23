from __future__ import annotations

from dataclasses import replace
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.providers.contracts import ProviderCode, validate_symbol
from long_invest.modules.providers.service import ProviderService
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response

router = APIRouter(prefix="/api/v1/providers", tags=["providers"])


def get_provider_service() -> ProviderService:
    raise RuntimeError("ProviderService must be supplied by the composition root")


ServiceDependency = Annotated[ProviderService, Depends(get_provider_service)]
ReadRequest = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]


async def require_provider_write_request(
    request: Annotated[
        AuthenticatedRequest, Depends(require_verified_write_request)
    ],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> AuthenticatedRequest:
    if idempotency_key is None or not idempotency_key.strip():
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="Provider 写操作必须提供 Idempotency-Key",
            status_code=422,
        )
    return replace(
        request,
        audit_context=replace(
            request.audit_context,
            idempotency_key=idempotency_key.strip(),
        ),
    )


WriteRequest = Annotated[
    AuthenticatedRequest, Depends(require_provider_write_request)
]


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool
    reason: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=20)
    concurrency: int | None = Field(default=None, ge=1, le=32)
    rate_per_second: float | None = Field(default=None, gt=0, le=100)
    timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    auto_switch: bool | None = None


class ConfirmedActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool
    reason: str = Field(min_length=1, max_length=255)


class QuoteDiagnosticsRequest(ConfirmedActionRequest):
    symbols: tuple[str, ...] = Field(min_length=1, max_length=100)


def require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="PROVIDER_CONFIRMATION_REQUIRED",
            message="请确认 Provider 操作",
            status_code=422,
        )


@router.get("")
async def list_providers(service: ServiceDependency, _request: ReadRequest) -> dict:
    return success_response(
        data=[_provider(item) for item in await service.list_providers()]
    )


@router.get("/circuits")
async def list_circuits(service: ServiceDependency, _request: ReadRequest) -> dict:
    return success_response(
        data=[_circuit(item) for item in await service.list_circuits()]
    )


@router.post("/circuits/{circuit_id}/probe")
async def probe_circuit(
    circuit_id: UUID,
    body: ConfirmedActionRequest,
    service: ServiceDependency,
    request: WriteRequest,
) -> dict:
    require_confirmation(body.confirm)
    return success_response(
        data=await service.probe_circuit(
            circuit_id, reason=body.reason, audit_context=request.audit_context
        )
    )


@router.post("/circuits/{circuit_id}/reset")
async def reset_circuit(
    circuit_id: UUID,
    body: ConfirmedActionRequest,
    service: ServiceDependency,
    request: WriteRequest,
) -> dict:
    require_confirmation(body.confirm)
    return success_response(
        data=await service.reset_circuit(
            circuit_id, reason=body.reason, audit_context=request.audit_context
        )
    )


@router.post("/quote-diagnostics")
async def quote_diagnostics(
    body: QuoteDiagnosticsRequest, service: ServiceDependency, request: WriteRequest
) -> dict:
    require_confirmation(body.confirm)
    for symbol in body.symbols:
        try:
            validate_symbol(symbol)
        except ValueError as error:
            raise AppError(
                code="PROVIDER_SYMBOL_INVALID", message="股票代码无效", status_code=422
            ) from error
    return success_response(
        data=await service.quote_diagnostics(
            body.symbols, reason=body.reason, audit_context=request.audit_context
        )
    )


@router.get("/{provider_code}")
async def get_provider(
    provider_code: ProviderCode, service: ServiceDependency, _request: ReadRequest
) -> dict:
    return success_response(data=_provider(await service.get_provider(provider_code)))


@router.get("/{provider_code}/capabilities")
async def capabilities(
    provider_code: ProviderCode, service: ServiceDependency, _request: ReadRequest
) -> dict:
    return success_response(data=await service.capabilities(provider_code))


@router.get("/{provider_code}/health")
async def health(
    provider_code: ProviderCode, service: ServiceDependency, _request: ReadRequest
) -> dict:
    return success_response(data=await service.health(provider_code))


@router.patch("/{provider_code}/settings")
async def update_settings(
    provider_code: ProviderCode,
    body: SettingsRequest,
    service: ServiceDependency,
    request: WriteRequest,
) -> dict:
    require_confirmation(body.confirm)
    settings = body.model_dump(
        exclude={"confirm", "reason", "expected_version"}, exclude_none=True
    )
    return success_response(
        data=await service.update_settings(
            provider_code,
            settings,
            expected_version=body.expected_version,
            reason=body.reason,
            audit_context=request.audit_context,
        )
    )


def _provider(item: dict) -> dict:
    return {
        **item,
        "allowed_actions": ["UPDATE_SETTINGS", "QUOTE_DIAGNOSTICS"],
    }


def _circuit(item: dict) -> dict:
    return {**item, "allowed_actions": ["PROBE", "RESET"]}
