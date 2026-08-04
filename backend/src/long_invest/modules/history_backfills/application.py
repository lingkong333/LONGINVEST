from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    HistoryBackfillAuditContext,
    HistoryDateRangePort,
    HistoryScopeSnapshotPort,
)
from long_invest.modules.history_backfills.service import HistoryBackfillService
from long_invest.modules.securities.application import SecurityApplication
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.admin import JobAdminService, JobCommandContext
from long_invest.platform.jobs.postgres_service import PostgresJobService
from long_invest.platform.jobs.service import JobService


class HistoryBackfillApplication:
    def __init__(
        self,
        database: Database,
        *,
        scope_snapshots: HistoryScopeSnapshotPort,
        job_service_factory: Callable[..., Any] = PostgresJobService,
        legacy_job_service_factory: Callable[..., Any] = JobService,
        date_ranges: HistoryDateRangePort | None = None,
        admin_service_factory: Callable[..., Any] = JobAdminService,
        audit_service_factory: Callable[..., Any] = AuditService,
    ) -> None:
        self._database = database
        self._scope_snapshots = scope_snapshots
        self._job_service_factory = job_service_factory
        self._legacy_job_service_factory = legacy_job_service_factory
        self._date_ranges = date_ranges
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

    async def item_status_counts_many(self, job_ids: tuple[UUID, ...]):
        try:
            async with self._database.session() as session:
                return await self._service(session).item_status_counts_many(job_ids)
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

    async def items(
        self,
        job_id: UUID,
        *,
        status: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        job = await self.get(job_id)
        snapshot_id = UUID(str(job.config_snapshot["universe_snapshot_id"]))
        frozen = await SecurityApplication(self._database).frozen_universe(snapshot_id)
        checkpoint = job.checkpoint or {}
        failures = {
            str(item.get("symbol")): item
            for item in checkpoint.get("failures", ())
        }
        anomalies = {
            str(item.get("symbol")): item
            for item in checkpoint.get("anomalies", ())
        }
        selected_symbols = tuple(
            str(item) for item in checkpoint.get("retry_items", ())
        )
        selected = (
            tuple(item for item in frozen.items if item.symbol in selected_symbols)
            if selected_symbols
            else frozen.items
        )
        cursor = int(checkpoint.get("cursor", 0))
        concurrency = int(job.config_snapshot.get("concurrency", 1))
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(selected):
            error = failures.get(item.symbol)
            anomaly = anomalies.get(item.symbol)
            if error is not None:
                item_status = "FAILED"
            elif anomaly is not None:
                item_status = "ANOMALY"
            elif index < cursor:
                item_status = "SUCCEEDED"
            elif str(job.status) == "RUNNING" and index < cursor + concurrency:
                item_status = "RUNNING"
            elif str(job.status) == "CANCELED":
                item_status = "CANCELED"
            else:
                item_status = "PENDING"
            rows.append(
                {
                    "security_id": str(item.security_id),
                    "symbol": item.symbol,
                    "status": item_status,
                    "error_code": error.get("error_code") if error else None,
                    "retryable": bool(error.get("retryable")) if error else True,
                    "anomaly_rows": anomaly.get("rows", []) if anomaly else [],
                }
            )
        if status:
            rows = [item for item in rows if item["status"] == status]
        total = len(rows)
        start = (page - 1) * page_size
        return {
            "items": rows[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def _service(self, session: Any) -> HistoryBackfillService:
        return HistoryBackfillService(
            scope_snapshots=self._scope_snapshots,
            jobs=self._job_service_factory(session),
            legacy_jobs=self._legacy_job_service_factory(session),
            date_ranges=self._date_ranges,
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
