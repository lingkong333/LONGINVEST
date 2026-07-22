from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.audit.models import AuditEvent


@dataclass(frozen=True, slots=True)
class AuditEventFilters:
    start_at: datetime
    end_at: datetime
    actor_user_id: str | None = None
    action_code: str | None = None
    object_type: str | None = None
    object_id: str | None = None
    result: str | None = None
    risk_level: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEventView:
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


@dataclass(frozen=True, slots=True)
class AuditEventPage:
    items: tuple[AuditEventView, ...]
    total: int
    page: int
    page_size: int


class AuditEventQuery:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_events(
        self,
        filters: AuditEventFilters,
        *,
        page: int,
        page_size: int,
    ) -> AuditEventPage:
        conditions = self._conditions(filters)
        total = await self._session.scalar(
            select(func.count()).select_from(AuditEvent).where(*conditions)
        )
        rows = (
            await self._session.scalars(
                select(AuditEvent)
                .where(*conditions)
                .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return AuditEventPage(
            items=tuple(_view(row) for row in rows),
            total=int(total or 0),
            page=page,
            page_size=page_size,
        )

    @staticmethod
    def _conditions(filters: AuditEventFilters) -> tuple[Any, ...]:
        conditions: list[Any] = [
            AuditEvent.occurred_at >= filters.start_at,
            AuditEvent.occurred_at <= filters.end_at,
        ]
        for column, value in (
            (AuditEvent.actor_user_id, filters.actor_user_id),
            (AuditEvent.action_code, filters.action_code),
            (AuditEvent.object_type, filters.object_type),
            (AuditEvent.object_id, filters.object_id),
            (AuditEvent.result, filters.result),
            (AuditEvent.risk_level, filters.risk_level),
            (AuditEvent.request_id, filters.request_id),
        ):
            if value is not None:
                conditions.append(column == value)
        return tuple(conditions)


def _view(event: AuditEvent) -> AuditEventView:
    return AuditEventView(
        id=event.id,
        occurred_at=event.occurred_at,
        actor_user_id=event.actor_user_id,
        session_id=event.session_id,
        trusted_ip=event.trusted_ip,
        action_code=event.action_code,
        object_type=event.object_type,
        object_id=event.object_id,
        result=event.result,
        before_summary=event.before_summary,
        after_summary=event.after_summary,
        reason=event.reason,
        request_id=event.request_id,
        idempotency_key=event.idempotency_key,
        risk_level=event.risk_level,
    )
