import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.securities.contracts import SecurityAuditContext
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService


@dataclass(frozen=True, slots=True)
class SecurityMasterAuditEvent:
    context: SecurityAuditContext
    master_version: int
    total_count: int
    created_count: int
    updated_count: int
    revision_count: int


@dataclass(frozen=True, slots=True)
class SecurityMasterUpdatedEvent:
    master_version: int
    source: str
    source_version: str
    total_count: int
    created_count: int
    updated_count: int


class SecurityAuditPort(Protocol):
    session: AsyncSession

    async def record(self, event: SecurityMasterAuditEvent) -> None: ...


class SecurityEventPort(Protocol):
    session: AsyncSession

    async def publish(self, event: SecurityMasterUpdatedEvent) -> None: ...


class TransactionBoundOutboxWriter(Protocol):
    async def append(
        self,
        *,
        session: AsyncSession,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        queue: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None: ...


class SecurityAuditAdapter(SecurityAuditPort):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._audit = AuditService(session)

    async def record(self, event: SecurityMasterAuditEvent) -> None:
        context = event.context
        digest = hashlib.sha256(context.idempotency_key.encode()).hexdigest()
        await self._audit.append(
            AuditWrite(
                action_code="SECURITY_MASTER_APPLY",
                object_type="security_master",
                object_id=str(event.master_version),
                result="SUCCESS",
                request_id=context.request_id,
                idempotency_key=f"securities:{digest}",
                risk_level="MEDIUM",
                reason=context.reason,
                before_summary=None,
                after_summary={
                    "master_version": event.master_version,
                    "total_count": event.total_count,
                    "created_count": event.created_count,
                    "updated_count": event.updated_count,
                    "revision_count": event.revision_count,
                },
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )


class TransactionalSecurityEventAdapter(SecurityEventPort):
    def __init__(
        self,
        session: AsyncSession,
        writer: TransactionBoundOutboxWriter,
    ) -> None:
        self.session = session
        self._writer = writer

    async def publish(self, event: SecurityMasterUpdatedEvent) -> None:
        digest = hashlib.sha256(
            f"{event.source}\0{event.source_version}".encode()
        ).hexdigest()
        await self._writer.append(
            session=self.session,
            topic="security_master.updated",
            aggregate_type="security_master",
            aggregate_id=str(event.master_version),
            queue="default",
            payload={
                "master_version": event.master_version,
                "source": event.source,
                "source_version": event.source_version,
                "total_count": event.total_count,
                "created_count": event.created_count,
                "updated_count": event.updated_count,
            },
            dedupe_key=f"security-master:{digest}",
        )
