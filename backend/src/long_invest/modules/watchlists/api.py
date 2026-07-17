from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.watchlists.application import (
    WatchlistApplication,
    WatchlistAuditContext,
    get_watchlist_application,
)
from long_invest.modules.watchlists.contracts import (
    WatchlistBatchInput,
    WatchlistBatchItem,
    WatchlistItemMutationResult,
    WatchlistItemRemovalResult,
    WatchlistMutation,
    WatchlistView,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(prefix="/api/v1/watchlists", tags=["watchlists"])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class WatchlistBody(StrictRequest):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    display_order: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)


class ArchiveBody(StrictRequest):
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=1)


class ItemBody(ArchiveBody):
    symbol: str = Field(min_length=1, max_length=16)
    source: str = Field(default="manual", min_length=1, max_length=64)


class RemoveItemBody(ArchiveBody):
    pass


class BatchBody(ArchiveBody):
    symbols: tuple[str, ...] = Field(min_length=1, max_length=200)
    source: str = Field(default="manual", min_length=1, max_length=64)


class WatchlistListData(BaseModel):
    items: list[WatchlistView]


class WatchlistListResponse(SuccessEnvelope):
    data: WatchlistListData


class WatchlistResponse(SuccessEnvelope):
    data: WatchlistView


class WatchlistItemResponse(SuccessEnvelope):
    data: WatchlistItemMutationResult


class WatchlistRemovalResponse(SuccessEnvelope):
    data: WatchlistItemRemovalResult


class WatchlistBatchData(BaseModel):
    items: list[WatchlistBatchItem]


class WatchlistBatchResponse(SuccessEnvelope):
    data: WatchlistBatchData


ApplicationDependency = Annotated[
    WatchlistApplication, Depends(get_watchlist_application)
]
ReadAuth = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteAuth = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]


def _idempotency_key(
    value: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=200)
    ] = None,
) -> str:
    normalized = value.strip() if value else ""
    if not normalized:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="写操作必须提供幂等键",
            status_code=422,
        )
    return normalized


IdempotencyKey = Annotated[str, Depends(_idempotency_key)]


@router.get("", response_model=WatchlistListResponse)
async def list_watchlists(
    application: ApplicationDependency,
    authenticated: ReadAuth,
    include_archived: bool = Query(False),
) -> dict:
    items = await application.list(
        owner_user_id=authenticated.user.id, include_archived=include_archived
    )
    return success_response(
        data={"items": [item.model_dump(mode="json") for item in items]}
    )


@router.post("", response_model=WatchlistResponse)
async def create_watchlist(
    body: WatchlistBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    result = await application.create(
        authenticated.user.id,
        _mutation(body, idempotency_key),
        audit_context=_context(authenticated),
    )
    return success_response(data=result.model_dump(mode="json"))


@router.get("/{watchlist_id}", response_model=WatchlistResponse)
async def get_watchlist(
    watchlist_id: UUID, application: ApplicationDependency, authenticated: ReadAuth
) -> dict:
    result = await application.get(watchlist_id, owner_user_id=authenticated.user.id)
    return success_response(data=result.model_dump(mode="json"))


@router.patch("/{watchlist_id}", response_model=WatchlistResponse)
async def update_watchlist(
    watchlist_id: UUID,
    body: WatchlistBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    result = await application.update(
        watchlist_id,
        owner_user_id=authenticated.user.id,
        command=_mutation(body, idempotency_key),
        audit_context=_context(authenticated),
    )
    return success_response(data=result.model_dump(mode="json"))


@router.delete("/{watchlist_id}", response_model=WatchlistResponse)
async def archive_watchlist(
    watchlist_id: UUID,
    body: ArchiveBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    result = await application.archive(
        watchlist_id,
        owner_user_id=authenticated.user.id,
        reason=body.reason,
        idempotency_key=idempotency_key,
        expected_version=body.expected_version,
        audit_context=_context(authenticated),
    )
    return success_response(data=result.model_dump(mode="json"))


@router.post("/{watchlist_id}/items", response_model=WatchlistItemResponse)
async def add_item(
    watchlist_id: UUID,
    body: ItemBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    result = await application.add_item(
        watchlist_id,
        owner_user_id=authenticated.user.id,
        symbol=body.symbol,
        source=body.source,
        reason=body.reason,
        idempotency_key=idempotency_key,
        expected_version=body.expected_version,
        audit_context=_context(authenticated),
    )
    return success_response(data=result.model_dump(mode="json"))


@router.post("/{watchlist_id}/items/batch", response_model=WatchlistBatchResponse)
async def add_batch(
    watchlist_id: UUID,
    body: BatchBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    results = await application.add_batch(
        watchlist_id,
        owner_user_id=authenticated.user.id,
        batch=WatchlistBatchInput(symbols=body.symbols),
        source=body.source,
        reason=body.reason,
        idempotency_key=idempotency_key,
        expected_version=body.expected_version,
        audit_context=_context(authenticated),
    )
    return success_response(
        data={"items": [item.model_dump(mode="json") for item in results]}
    )


@router.delete(
    "/{watchlist_id}/items/{symbol}", response_model=WatchlistRemovalResponse
)
async def remove_item(
    watchlist_id: UUID,
    symbol: str,
    body: RemoveItemBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict:
    result = await application.remove_item(
        watchlist_id,
        owner_user_id=authenticated.user.id,
        symbol=symbol,
        reason=body.reason,
        idempotency_key=idempotency_key,
        expected_version=body.expected_version,
        audit_context=_context(authenticated),
    )
    return success_response(data=result.model_dump(mode="json"))


def _mutation(body: WatchlistBody, key: str) -> WatchlistMutation:
    return WatchlistMutation(
        name=body.name,
        description=body.description,
        display_order=body.display_order,
        reason=body.reason,
        idempotency_key=key,
        expected_version=body.expected_version,
    )


def _context(authenticated: AuthenticatedRequest) -> WatchlistAuditContext:
    audit = authenticated.audit_context
    return WatchlistAuditContext(
        request_id=audit.request_id,
        actor_user_id=str(authenticated.user.id),
        session_id=str(authenticated.session.id),
        trusted_ip=audit.trusted_ip or "unknown",
    )
