from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    HistoryBackfillAuditContext,
    HistoryScopeSnapshotPort,
)
from long_invest.modules.history_backfills.service import HistoryBackfillService
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.admin import JobAdminService, JobCommandContext
from long_invest.platform.jobs.service import JobService


class HistoryBackfillApplication:
    def __init__(
        self,
        database: Database,
        *,
        scope_snapshots: HistoryScopeSnapshotPort,
        job_service_factory: Callable[..., Any] = JobService,
        admin_service_factory: Callable[..., Any] = JobAdminService,
        audit_service_factory: Callable[..., Any] = AuditService,
    ) -> None:
        self._database = database
        self._scope_snapshots = scope_snapshots
        self._job_service_factory = job_service_factory
        self._admin_service_factory = admin_service_factory
        self._audit_service_factory = audit_service_factory

    async def create(
        self,
        command: CreateHistoryBackfill,
        context: HistoryBackfillAuditContext,
        *,
        owner_user_id: UUID,
    ) -> Any:
        try:
            async with self._database.transaction() as session:
                return await self._service(session).create(
                    session, command, context, owner_user_id=owner_user_id
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def list(self, *, page: int, page_size: int):
        try:
            async with self._database.session() as session:
                return await self._service(session).list(page=page, page_size=page_size)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def get(self, job_id: UUID):
        try:
            async with self._database.session() as session:
                return await self._service(session).get(job_id)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def allowed_actions(self, job_id: UUID):
        try:
            async with self._database.session() as session:
                return await self._service(session).allowed_actions(job_id)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def allowed_actions_many(self, job_ids: tuple[UUID, ...]):
        try:
            async with self._database.session() as session:
                return await self._service(session).allowed_actions_many(job_ids)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def command(
        self,
        job_id: UUID,
        action: str,
        context: JobCommandContext,
    ):
        try:
            async with self._database.transaction() as session:
                return await self._service(session).command(job_id, action, context)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    def _service(self, session: Any) -> HistoryBackfillService:
        return HistoryBackfillService(
            scope_snapshots=self._scope_snapshots,
            jobs=self._job_service_factory(session),
            admin=self._admin_service_factory(session),
            audit=self._audit_service_factory(session),
        )


_application_factory: Callable[[], HistoryBackfillApplication] | None = None


def configure_history_backfill_application(
    factory: Callable[[], HistoryBackfillApplication],
) -> None:
    global _application_factory
    _application_factory = factory


def get_history_backfill_application() -> HistoryBackfillApplication:
    if _application_factory is None:
        raise AppError(
            code="HISTORY_BACKFILL_NOT_CONFIGURED",
            message="历史回填尚未完成生产装配",
            status_code=503,
        )
    return _application_factory()


def _backend_unavailable() -> AppError:
    return AppError(
        code="HISTORY_BACKFILL_BACKEND_UNAVAILABLE",
        message="历史回填服务暂时不可用",
        status_code=503,
    )
