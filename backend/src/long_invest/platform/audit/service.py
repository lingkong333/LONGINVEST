from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.audit.contracts import AuditRecord, AuditWrite
from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.audit.repository import AuditRepository


class AuditService:
    """Public transaction-bound access to the append-only audit store."""

    def __init__(self, session: AsyncSession) -> None:
        self._repository = AuditRepository(session)

    async def append(self, data: AuditWrite) -> AuditRecord:
        return _record(await self._repository.append(data))

    async def find_by_idempotency(self, idempotency_key: str) -> AuditRecord | None:
        event = await self._repository.find_by_idempotency(idempotency_key)
        return _record(event) if event is not None else None


def _record(event: AuditEvent) -> AuditRecord:
    return AuditRecord(
        action_code=event.action_code,
        object_type=event.object_type,
        object_id=event.object_id,
        result=event.result,
        request_id=event.request_id,
        idempotency_key=event.idempotency_key,
        risk_level=event.risk_level,
        reason=event.reason,
        before_summary=event.before_summary,
        after_summary=event.after_summary,
        actor_user_id=event.actor_user_id,
        session_id=event.session_id,
        trusted_ip=event.trusted_ip,
    )
