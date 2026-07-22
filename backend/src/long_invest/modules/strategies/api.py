from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.strategies.application import (
    StrategyApplication,
    get_strategy_application,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])
Application = Annotated[StrategyApplication, Depends(get_strategy_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[
    AuthenticatedRequest, Depends(require_verified_write_request)
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfirmedRequest(StrictRequest):
    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=200)


class CreateStrategyRequest(ConfirmedRequest):
    name: str = Field(min_length=1, max_length=100)


class RenameStrategyRequest(CreateStrategyRequest):
    expected_version: int = Field(ge=1)


class SaveDraftRequest(ConfirmedRequest):
    source_code: str
    expected_version: int = Field(ge=1)


class RestoreRevisionRequest(ConfirmedRequest):
    expected_version: int = Field(ge=1)


class ArchiveStrategyRequest(ConfirmedRequest):
    expected_version: int = Field(ge=1)


class PublishStrategyRequest(ConfirmedRequest):
    validation_run_id: UUID
    expected_draft_version: int = Field(ge=1)


class ValidateStrategyRequest(ConfirmedRequest):
    backtest_task_id: UUID
    metadata: dict[str, Any]
    parameter_schema: dict[str, Any]
    params: dict[str, Any]


def idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if not value or len(value) > 160:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="策略写操作需要有效的幂等键",
            status_code=422,
        )
    return value


IdempotencyKey = Annotated[str, Depends(idempotency_key)]


@router.get("")
async def list_strategies(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    include_archived: bool = False,
) -> dict[str, Any]:
    rows, total = await application.list(
        page=page, page_size=page_size, include_archived=include_archived
    )
    return success_response(
        data={
            "items": [_strategy(row) for row in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.post("")
async def create_strategy(
    body: CreateStrategyRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    result = await application.create(
        name=body.name.strip(), **_context(identity, key, body.reason)
    )
    return success_response(
        data={
            "strategy": _strategy(result.strategy),
            "draft": _draft(result.draft),
        },
        code="STRATEGY_CREATED",
        message="策略已创建",
    )


@router.get("/{strategy_id}")
async def get_strategy(
    strategy_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data=_strategy(await application.get(strategy_id)))


@router.patch("/{strategy_id}")
async def rename_strategy(
    strategy_id: UUID,
    body: RenameStrategyRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    row = await application.rename(
        strategy_id,
        name=body.name.strip(),
        expected_version=body.expected_version,
        **_context(identity, key, body.reason),
    )
    return success_response(data=_strategy(row), code="STRATEGY_UPDATED")


@router.post("/{strategy_id}/archive")
async def archive_strategy(
    strategy_id: UUID,
    body: ArchiveStrategyRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    row = await application.archive(
        strategy_id,
        expected_version=body.expected_version,
        **_context(identity, key, body.reason),
    )
    return success_response(data=_strategy(row), code="STRATEGY_ARCHIVED")


@router.post("/{strategy_id}/restore")
async def restore_strategy(
    strategy_id: UUID,
    body: ArchiveStrategyRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    row = await application.restore(
        strategy_id,
        expected_version=body.expected_version,
        **_context(identity, key, body.reason),
    )
    return success_response(data=_strategy(row), code="STRATEGY_RESTORED")


@router.get("/{strategy_id}/draft")
async def get_draft(
    strategy_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data=_draft(await application.get_draft(strategy_id)))


@router.put("/{strategy_id}/draft")
async def save_draft(
    strategy_id: UUID,
    body: SaveDraftRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    row = await application.save_draft(
        strategy_id,
        source_code=body.source_code,
        expected_version=body.expected_version,
        create_revision=False,
        **_context(identity, key, body.reason),
    )
    return success_response(data=_draft(row), code="STRATEGY_DRAFT_SAVED")


@router.post("/{strategy_id}/draft/revisions")
async def create_draft_revision(
    strategy_id: UUID,
    body: SaveDraftRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    row = await application.save_draft(
        strategy_id,
        source_code=body.source_code,
        expected_version=body.expected_version,
        create_revision=True,
        **_context(identity, key, body.reason),
    )
    return success_response(
        data=_draft(row), code="STRATEGY_DRAFT_REVISION_CREATED"
    )


@router.get("/{strategy_id}/draft/revisions")
async def list_draft_revisions(
    strategy_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    rows, total = await application.list_revisions(
        strategy_id, page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [_revision(row) for row in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


@router.post("/{strategy_id}/draft/revisions/{revision_id}/restore")
async def restore_draft_revision(
    strategy_id: UUID,
    revision_id: UUID,
    body: RestoreRevisionRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    row = await application.restore_revision(
        strategy_id,
        revision_id=revision_id,
        expected_version=body.expected_version,
        **_context(identity, key, body.reason),
    )
    return success_response(data=_draft(row), code="STRATEGY_DRAFT_RESTORED")


@router.get("/{strategy_id}/diff")
async def get_draft_diff(
    strategy_id: UUID,
    revision_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    value = await application.diff(strategy_id, revision_id=revision_id)
    return success_response(data={"diff": value})


@router.post("/{strategy_id}/publish", status_code=202)
async def publish_strategy(
    strategy_id: UUID,
    body: PublishStrategyRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    publication = await application.publish(
        strategy_id=strategy_id,
        validation_run_id=body.validation_run_id,
        expected_draft_version=body.expected_draft_version,
        **_context(identity, key, body.reason),
    )
    if publication.run is None:
        raise AppError(
            code="STRATEGY_PUBLISH_STATE_UNCERTAIN",
            message="发布任务记录缺失",
            status_code=503,
        )
    return success_response(
        data={
            "run_id": str(publication.run.id),
            "version_id": str(publication.version.id),
            "status": str(publication.run.status),
        },
        code="STRATEGY_PUBLISH_REQUESTED",
        message="策略发布任务已提交",
    )


@router.post("/{strategy_id}/validate", status_code=202)
async def validate_strategy(
    strategy_id: UUID,
    body: ValidateStrategyRequest,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirm(body)
    row = await application.request_validation(
        strategy_id,
        backtest_task_id=body.backtest_task_id,
        metadata=body.metadata,
        parameter_schema=body.parameter_schema,
        params=body.params,
        **_context(identity, key, body.reason),
    )
    return success_response(
        data=_validation_run(row),
        code="STRATEGY_VALIDATION_REQUESTED",
        message="策略验证已提交",
    )


@router.get("/{strategy_id}/versions")
async def list_versions(
    strategy_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    rows, total = await application.list_versions(
        strategy_id, page=page, page_size=page_size
    )
    return success_response(
        data={
            "items": [_version(row) for row in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


def _confirm(body: ConfirmedRequest) -> None:
    if not body.confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认本次策略写操作",
            status_code=422,
        )
    if not body.reason.strip():
        raise AppError(
            code="STRATEGY_INPUT_INVALID",
            message="操作原因不能为空",
            status_code=422,
        )


def _context(
    identity: AuthenticatedRequest, key: str, reason: str
) -> dict[str, str]:
    return {
        "reason": reason.strip(),
        "idempotency_key": key,
        "request_id": identity.audit_context.request_id,
        "actor_user_id": str(identity.user.id),
        "session_id": str(identity.session.id),
        "trusted_ip": identity.audit_context.trusted_ip or "unknown",
    }


def _strategy(row: Any) -> dict[str, Any]:
    return {"id": str(row.id), "name": row.name, "status": str(row.status)}


def _draft(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "strategy_id": str(row.strategy_id),
        "draft_version": row.draft_version,
        "source_code": row.source_code,
    }


def _revision(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "draft_id": str(row.draft_id),
        "revision_no": row.revision_no,
        "source_code": row.source_code,
    }


def _version(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "strategy_id": str(row.strategy_id),
        "version_no": row.version_no,
        "source_code_hash": row.source_code_hash,
        "metadata": row.strategy_metadata,
        "parameter_schema": row.parameter_schema,
        "environment_version": row.environment_version,
        "runner_image_digest": row.runner_image_digest,
        "git_commit": row.git_commit,
        "status": str(row.status),
        "published_at": (
            row.published_at.isoformat() if row.published_at is not None else None
        ),
    }


def _validation_run(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "strategy_id": str(row.strategy_id),
        "strategy_version_id": (
            str(row.strategy_version_id)
            if row.strategy_version_id is not None
            else None
        ),
        "draft_version": row.draft_version,
        "source_code_hash": row.source_code_hash,
        "status": str(row.status),
        "error_code": row.error_code,
        "created_at": (
            row.created_at.isoformat() if row.created_at is not None else None
        ),
        "completed_at": (
            row.completed_at.isoformat() if row.completed_at is not None else None
        ),
    }
