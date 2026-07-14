from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.audit.models import AuditEvent


@dataclass(frozen=True, slots=True)
class NewAuditEvent:
    action_code: str
    object_type: str
    object_id: str
    result: str
    request_id: str
    idempotency_key: str
    risk_level: str
    reason: str | None
    before_summary: dict[str, Any] | None
    after_summary: dict[str, Any] | None
    actor_user_id: str | None = None
    session_id: str | None = None
    trusted_ip: str | None = None


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, data: NewAuditEvent) -> AuditEvent:
        event = AuditEvent(
            action_code=data.action_code,
            object_type=data.object_type,
            object_id=data.object_id,
            result=data.result,
            request_id=data.request_id,
            idempotency_key=data.idempotency_key,
            risk_level=data.risk_level,
            reason=data.reason,
            before_summary=data.before_summary,
            after_summary=data.after_summary,
            actor_user_id=data.actor_user_id,
            session_id=data.session_id,
            trusted_ip=data.trusted_ip,
        )
        self._session.add(event)
        await self._session.flush()
        return event

