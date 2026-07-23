from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.market_data.contracts import (
    QualityIssuePage,
    QualityIssueStatus,
    QualityIssueView,
    QualityResolutionAction,
    QualitySeverity,
)
from long_invest.modules.market_data.quality_application import (
    QualityAuditContext,
    QualityIssueApplication,
    get_quality_issue_application,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(prefix="/api/v1/data-quality/issues", tags=["data-quality"])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ConfirmedReasonBody(StrictRequest):
    confirm: bool
    reason: str = Field(min_length=1, max_length=500)


class SelectSourceBody(ConfirmedReasonBody):
    selected_source: str = Field(min_length=1, max_length=64)


class QualityIssueItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    issue_type: str
    subject_type: str
    subject_id: str
    symbol: str | None
    status: QualityIssueStatus
    severity: QualitySeverity
    evidence: dict[str, Any]
    source_candidates: list[str]
    allowed_actions: list[QualityResolutionAction]
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    resolved_by_user_id: str | None
    resolution_action: QualityResolutionAction | None
    resolution_reason: str | None
    selected_source: str | None


class QualityIssueListData(BaseModel):
    items: list[QualityIssueItem]
    pagination: Pagination


class QualityIssueListResponse(SuccessEnvelope):
    data: QualityIssueListData


class QualityIssueResponse(SuccessEnvelope):
    data: QualityIssueItem


ApplicationDependency = Annotated[
    QualityIssueApplication, Depends(get_quality_issue_application)
]
ReadAuth = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteAuth = Annotated[AuthenticatedRequest, Depends(require_verified_write_request)]


def _idempotency_key(
    value: Annotated[str, Header(alias="Idempotency-Key", max_length=160)],
) -> str:
    normalized = value.strip()
    if not normalized:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="写操作必须提供幂等键",
            status_code=422,
        )
    return normalized


IdempotencyKey = Annotated[str, Depends(_idempotency_key)]


@router.get("", response_model=QualityIssueListResponse)
async def list_quality_issues(
    application: ApplicationDependency,
    _authenticated: ReadAuth,
    status: QualityIssueStatus | None = None,
    issue_type: Annotated[str | None, Query(max_length=100)] = None,
    symbol: Annotated[str | None, Query(max_length=16)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    result = await application.list(
        status=status,
        issue_type=issue_type,
        symbol=symbol,
        page=page,
        page_size=page_size,
    )
    return _page_response(result)


@router.get("/{issue_id}", response_model=QualityIssueResponse)
async def get_quality_issue(
    issue_id: UUID,
    application: ApplicationDependency,
    _authenticated: ReadAuth,
) -> dict[str, Any]:
    return _issue_response(await application.get(issue_id))


@router.post("/{issue_id}/select-source", response_model=QualityIssueResponse)
async def select_source(
    issue_id: UUID,
    body: SelectSourceBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    result = await application.select_source(
        issue_id,
        selected_source=body.selected_source,
        reason=body.reason,
        idempotency_key=idempotency_key,
        audit_context=_context(authenticated),
    )
    return _issue_response(result.issue)


@router.post("/{issue_id}/resolve", response_model=QualityIssueResponse)
async def invalidate_issue(
    issue_id: UUID,
    body: ConfirmedReasonBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    result = await application.invalidate(
        issue_id,
        reason=body.reason,
        idempotency_key=idempotency_key,
        audit_context=_context(authenticated),
    )
    return _issue_response(result.issue)


@router.post(
    "/{issue_id}/refetch",
    response_model=QualityIssueResponse,
    status_code=http_status.HTTP_202_ACCEPTED,
)
async def refetch_issue(
    issue_id: UUID,
    body: ConfirmedReasonBody,
    application: ApplicationDependency,
    authenticated: WriteAuth,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    _require_confirmation(body.confirm)
    issue = await application.request_refetch(
        issue_id,
        reason=body.reason,
        idempotency_key=idempotency_key,
        audit_context=_context(authenticated),
    )
    return _issue_response(issue, code="REFETCH_ACCEPTED", message="重新抓取请求已受理")


def _page_response(page: QualityIssuePage) -> dict[str, Any]:
    return success_response(
        data={
            "items": [_item(item) for item in page.items],
            "pagination": {
                "page": page.page,
                "page_size": page.page_size,
                "total": page.total,
            },
        }
    )


def _issue_response(
    issue: QualityIssueView,
    *,
    code: str = "OK",
    message: str = "操作成功",
) -> dict[str, Any]:
    return success_response(data=_item(issue), code=code, message=message)


def _item(issue: QualityIssueView) -> dict[str, Any]:
    payload = QualityIssueItem.model_validate(
        {
            **asdict(issue),
            "source_candidates": _source_candidates(issue.evidence),
            "allowed_actions": _allowed_actions(issue),
        }
    )
    return payload.model_dump(mode="json")


def _source_candidates(evidence: dict[str, Any]) -> list[str]:
    sources = evidence.get("sources")
    if not isinstance(sources, dict):
        return []
    return sorted(str(source) for source in sources)


def _allowed_actions(issue: QualityIssueView) -> list[QualityResolutionAction]:
    if issue.status not in {
        QualityIssueStatus.OPEN,
        QualityIssueStatus.REVIEW_REQUIRED,
    }:
        return []
    actions = [
        QualityResolutionAction.INVALIDATE,
        QualityResolutionAction.REFETCH,
    ]
    if _source_candidates(issue.evidence):
        actions.insert(0, QualityResolutionAction.SELECT_SOURCE)
    return actions


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认数据质量操作",
            status_code=422,
        )


def _context(authenticated: AuthenticatedRequest) -> QualityAuditContext:
    audit = authenticated.audit_context
    return QualityAuditContext(
        request_id=audit.request_id,
        actor_user_id=str(authenticated.user.id),
        session_id=str(authenticated.session.id),
        trusted_ip=audit.trusted_ip or "unknown",
    )
