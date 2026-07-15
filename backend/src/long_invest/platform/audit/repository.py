from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.models import AuditEvent

NewAuditEvent = AuditWrite


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, data: AuditWrite) -> AuditEvent:
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

    async def find_by_idempotency(self, idempotency_key: str) -> AuditEvent | None:
        return await self._session.scalar(
            select(AuditEvent).where(AuditEvent.idempotency_key == idempotency_key)
        )
