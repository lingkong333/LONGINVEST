from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
)
from long_invest.platform.audit.application import AuditQueryApplication
from long_invest.platform.audit.query import AuditEventPage, AuditEventView
from long_invest.platform.database.engine import get_database
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope

router = APIRouter(prefix="/api/v1", tags=["audit"])


@lru_cache
def get_audit_query_application() -> AuditQueryApplication:
    return AuditQueryApplication(get_database())


Application = Annotated[AuditQueryApplication, Depends(get_audit_query_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]


class AuditEventResponse(BaseModel):
    id: UUID
    occurred_at: datetime
    actor_user_id: str | None
    session_id: str | None
    trusted_ip: str | None
    action_code: str
    object_type: str
    object_id: str
    result: str
    before_summary: dict[str, Any] | None
    after_summary: dict[str, Any] | None
    reason: str | None
    request_id: str
    idempotency_key: str
    risk_level: str


class AuditEventPageResponse(BaseModel):
    items: list[AuditEventResponse]
    pagination: Pagination
    allowed_actions: list[str]


class AuditEventPageEnvelope(SuccessEnvelope):
    data: AuditEventPageResponse


@router.get("/audit-events", response_model=AuditEventPageEnvelope)
async def list_audit_events(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    actor_user_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    action_code: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    object_type: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    object_id: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    result: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    risk_level: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    request_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> dict[str, Any]:
    result_page = await application.list_events(
        page=page,
        page_size=page_size,
        start_at=start_at,
        end_at=end_at,
        actor_user_id=actor_user_id,
        action_code=action_code,
        object_type=object_type,
        object_id=object_id,
        result=result,
        risk_level=risk_level,
        request_id=request_id,
    )
    return success_response(data=_page(result_page))


def _page(result: AuditEventPage) -> dict[str, Any]:
    return {
        "items": [_event(item) for item in result.items],
        "pagination": {
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
        },
        "allowed_actions": [],
    }


def _event(event: AuditEventView) -> dict[str, Any]:
    return {
        "id": event.id,
        "occurred_at": event.occurred_at,
        "actor_user_id": event.actor_user_id,
        "session_id": event.session_id,
        "trusted_ip": event.trusted_ip,
        "action_code": event.action_code,
        "object_type": event.object_type,
        "object_id": event.object_id,
        "result": event.result,
        "before_summary": event.before_summary,
        "after_summary": event.after_summary,
        "reason": event.reason,
        "request_id": event.request_id,
        "idempotency_key": event.idempotency_key,
        "risk_level": event.risk_level,
    }
