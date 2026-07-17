from __future__ import annotations

from collections.abc import Callable
from datetime import time
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from long_invest.modules.monitor_schedules.contracts import (
    MonitorScheduleView,
    ScheduleDefinition,
    ScheduleRevisionView,
)
from long_invest.modules.monitor_schedules.outbox import MonitorScheduleOutboxAdapter
from long_invest.modules.monitor_schedules.repository import MonitorScheduleRepository
from long_invest.modules.monitor_schedules.service import (
    MonitorScheduleService,
    ScheduleReplay,
)
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


class ScheduleAuditAdapter:
    def __init__(self, session: Any) -> None:
        self.session = session
        self._audit = AuditService(session)

    async def find_replay(
        self,
        *,
        action: str,
        schedule_id: UUID | None,
        idempotency_key: str,
    ) -> ScheduleReplay | None:
        existing = await self._audit.find_by_idempotency(
            _audit_key(schedule_id, idempotency_key, action)
        )
        if existing is None:
            return None
        summary = dict(existing.after_summary or {})
        digest = summary.pop("_request_digest", None)
        revision_id = summary.get("revision_id")
        if not isinstance(digest, str) or not isinstance(revision_id, str):
            raise AppError(
                code="MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT",
                message="已有幂等记录不完整",
                status_code=409,
            )
        return ScheduleReplay(
            schedule_id=UUID(existing.object_id),
            revision_id=UUID(revision_id),
            request_digest=digest,
            after_summary=summary,
        )

    async def record(self, event: Any) -> None:
        await self._audit.append(
            AuditWrite(
                action_code=f"monitor_schedule.{event.action}",
                object_type="monitor_schedule",
                object_id=str(event.schedule_id),
                result="SUCCESS",
                request_id=event.request_id,
                idempotency_key=_audit_key(
                    event.schedule_id,
                    event.idempotency_key,
                    event.action,
                ),
                risk_level="HIGH",
                reason=event.reason,
                before_summary=event.before_summary,
                after_summary={
                    **event.after_summary,
                    "_request_digest": event.request_digest,
                },
                actor_user_id=event.actor_user_id,
                session_id=event.session_id,
                trusted_ip=event.trusted_ip,
            )
        )


class MonitorScheduleApplication:
    def __init__(
        self,
        database: Database,
        *,
        repository_factory: Callable[..., Any] = MonitorScheduleRepository,
        service_factory: Callable[..., Any] = MonitorScheduleService,
        audit_factory: Callable[..., Any] = ScheduleAuditAdapter,
        event_factory: Callable[..., Any] = MonitorScheduleOutboxAdapter,
    ) -> None:
        self._database = database
        self._repository_factory = repository_factory
        self._service_factory = service_factory
        self._audit_factory = audit_factory
        self._event_factory = event_factory

    def _service(self, session: Any) -> Any:
        return self._service_factory(
            self._repository_factory(session),
            audit=self._audit_factory(session),
            events=self._event_factory(session),
        )

    async def list(
        self, *, include_archived: bool = False
    ) -> list[MonitorScheduleView]:
        try:
            async with self._database.session() as session:
                rows = await self._service(session).list(
                    include_archived=include_archived
                )
                return [_schedule_view(row) for row in rows]
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def get(self, schedule_id: UUID) -> MonitorScheduleView:
        try:
            async with self._database.session() as session:
                return _schedule_view(await self._service(session).get(schedule_id))
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def current_revision(self, schedule_id: UUID) -> ScheduleRevisionView:
        try:
            async with self._database.session() as session:
                return _revision_view(
                    await self._service(session).current_revision(schedule_id)
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def versions(self, schedule_id: UUID) -> list[ScheduleRevisionView]:
        try:
            async with self._database.session() as session:
                rows = await self._service(session).revisions(schedule_id)
                return [_revision_view(row) for row in rows]
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def create(self, definition: ScheduleDefinition, **context: str):
        return await self._write("create", definition, **context)

    async def update(
        self, schedule_id: UUID, definition: ScheduleDefinition, **context: str
    ):
        return await self._write("update", schedule_id, definition, **context)

    async def archive(self, schedule_id: UUID, **kwargs: Any):
        return await self._write("archive", schedule_id, **kwargs)

    async def restore(self, schedule_id: UUID, **kwargs: Any):
        return await self._write("restore", schedule_id, **kwargs)

    async def _write(self, method: str, *args: Any, **kwargs: Any):
        try:
            async with self._database.transaction() as session:
                result = await getattr(self._service(session), method)(*args, **kwargs)
                return {
                    "schedule": _schedule_view(result.schedule),
                    "revision": _revision_view(result.revision),
                    "replayed": result.replayed,
                }
        except AppError:
            raise
        except IntegrityError as exc:
            raise AppError(
                code="MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT",
                message="调度请求与已有请求冲突",
                status_code=409,
            ) from exc
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def get_monitor_schedule_application() -> MonitorScheduleApplication:
    return MonitorScheduleApplication(get_database())


def _schedule_view(row: Any) -> MonitorScheduleView:
    return MonitorScheduleView(
        id=row.id,
        name=row.name,
        current_revision_id=row.current_revision_id,
        version=row.version,
        archived_at=row.archived_at,
    )


def _revision_view(row: Any) -> ScheduleRevisionView:
    return ScheduleRevisionView(
        id=row.id,
        schedule_id=row.schedule_id,
        revision_no=row.revision_no,
        times=tuple(
            time.fromisoformat(value) if isinstance(value, str) else value
            for value in row.times
        ),
        timezone=row.timezone,
        reason=row.reason,
        created_at=row.created_at,
    )


def _backend_unavailable() -> AppError:
    return AppError(
        code="MONITOR_SCHEDULE_BACKEND_UNAVAILABLE",
        message="监控调度服务暂时不可用",
        status_code=503,
    )


def _audit_key(schedule_id: UUID | None, idempotency_key: str, action: str) -> str:
    digest = sha256(idempotency_key.encode()).hexdigest()
    if action == "created":
        return f"monitor-schedule:create:{digest}"
    return f"monitor-schedule:{schedule_id}:{digest}"
