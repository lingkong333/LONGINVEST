from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.history_backfills.application import (
    HistoryBackfillApplication,
    get_history_backfill_application,
)
from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    HistoryBackfillAuditContext,
    HistoryBackfillScope,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope
from long_invest.platform.jobs.admin import JobCommandContext

router = APIRouter(prefix="/api/v1/market-history/backfills", tags=["market-history"])
Application = Annotated[
    HistoryBackfillApplication, Depends(get_history_backfill_application)
]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]


def idempotency_key(
    value: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=160),
    ],
) -> str:
    key = value.strip()
    if not key:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="历史回填写操作需要有效的幂等键",
            status_code=422,
        )
    return key


IdempotencyKey = Annotated[str, Depends(idempotency_key)]


class CreateBackfillBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: HistoryBackfillScope
    start_date: date
    end_date: date
    concurrency: int = Field(default=4, ge=1, le=8)
    symbols: list[str] = Field(default_factory=list)
    watchlist_id: UUID | None = None
    confirm: StrictBool
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]

    @model_validator(mode="after")
    def validate_scope(self) -> CreateBackfillBody:
        try:
            self.to_command()
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def to_command(self) -> CreateHistoryBackfill:
        return CreateHistoryBackfill(
            scope=self.scope,
            start_date=self.start_date,
            end_date=self.end_date,
            concurrency=self.concurrency,
            symbols=tuple(self.symbols),
            watchlist_id=self.watchlist_id,
        )


class BackfillControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: StrictBool
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    expected_version: int = Field(ge=1)


class BackfillProgress(BaseModel):
    completed: int = Field(ge=0)
    total: int = Field(ge=0)
    message: str | None = None


class BackfillResultCounts(BaseModel):
    total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    canceled: int = Field(ge=0)
    pending: int = Field(ge=0)


class BackfillResult(BaseModel):
    success: bool
    code: str
    message: str
    retryable: bool
    data: BackfillResultCounts | None = None
    warnings: list[str] = Field(default_factory=list)


class BackfillScopeItem(BaseModel):
    security_id: UUID
    symbol: str


class BackfillScopeSnapshot(BaseModel):
    scope: HistoryBackfillScope
    requested_symbols: list[str]
    requested_watchlist_id: UUID | None
    universe_snapshot_id: UUID
    universe_master_version: int = Field(ge=1)
    start_date: date
    end_date: date
    concurrency: int = Field(ge=1, le=8)
    reason: str
    items: list[BackfillScopeItem]


class BackfillView(BaseModel):
    job_id: UUID
    status: str
    progress: BackfillProgress | None
    result_summary: BackfillResult | None
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None
    scope_snapshot: BackfillScopeSnapshot | None = None


class BackfillPageData(BaseModel):
    items: list[BackfillView]
    pagination: Pagination


class BackfillPageResponse(SuccessEnvelope):
    data: BackfillPageData


class BackfillResponse(SuccessEnvelope):
    data: BackfillView


@router.post("", status_code=202, response_model=BackfillResponse)
async def create_backfill(
    body: CreateBackfillBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, object]:
    if not body.confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认本次历史回填操作",
            status_code=422,
        )
    audit = identity.audit_context
    session = getattr(identity, "session", None)
    job = await application.create(
        body.to_command(),
        HistoryBackfillAuditContext(
            request_id=audit.request_id,
            idempotency_key=key,
            actor_user_id=str(identity.user.id),
            session_id=str(session.id) if session is not None else None,
            trusted_ip=getattr(audit, "trusted_ip", None),
            reason=body.reason.strip(),
        ),
        owner_user_id=identity.user.id,
    )
    return success_response(
        data=_job(job, detail=True),
        code="HISTORY_BACKFILL_ACCEPTED",
        message="历史回填任务已受理",
    )


@router.get("", response_model=BackfillPageResponse)
async def list_backfills(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    result = await application.list(page=page, page_size=page_size)
    return success_response(
        data={
            "items": [_job(item) for item in result.items],
            "pagination": {
                "page": result.page,
                "page_size": result.page_size,
                "total": result.total,
            },
        }
    )


@router.get("/{job_id}", response_model=BackfillResponse)
async def get_backfill(
    job_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, object]:
    return success_response(data=_job(await application.get(job_id), detail=True))


def _command_route(action: str):
    async def endpoint(
        job_id: UUID,
        body: BackfillControlBody,
        application: Application,
        identity: WriteIdentity,
        key: IdempotencyKey,
    ) -> dict[str, object]:
        if not body.confirm:
            raise AppError(
                code="AUTH_CONFIRMATION_REQUIRED",
                message="请确认本次历史回填控制操作",
                status_code=422,
            )
        audit = identity.audit_context
        session = getattr(identity, "session", None)
        job = await application.command(
            job_id,
            action,
            JobCommandContext(
                request_id=audit.request_id,
                idempotency_key=key,
                actor_user_id=str(identity.user.id),
                reason=body.reason.strip(),
                expected_version=body.expected_version,
                session_id=str(session.id) if session is not None else None,
                trusted_ip=getattr(audit, "trusted_ip", None),
            ),
        )
        return success_response(
            data=_job(job),
            code="HISTORY_BACKFILL_CONTROL_ACCEPTED",
            message="历史回填控制请求已受理",
        )

    return endpoint


for _action in ("pause", "resume", "cancel", "retry-failed"):
    router.add_api_route(
        f"/{{job_id}}/{_action}",
        _command_route(_action),
        methods=["POST"],
        status_code=202,
        response_model=BackfillResponse,
        name=f"history_backfill_{_action.replace('-', '_')}",
    )


def _job(job: Any, *, detail: bool = False) -> dict[str, object]:
    progress = job.progress or None
    if progress is not None and not {"completed", "total"}.issubset(progress):
        progress = None
    result = {
        "job_id": str(job.id),
        "status": str(job.status),
        "progress": progress,
        "result_summary": job.result_summary,
        "version": job.version,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "terminal_at": job.terminal_at,
    }
    if detail:
        result["scope_snapshot"] = job.config_snapshot
    return result
