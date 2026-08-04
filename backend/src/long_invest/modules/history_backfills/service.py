from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    HistoryBackfillAction,
    HistoryBackfillAuditContext,
    HistoryDateRangePort,
    HistoryScopeSnapshotPort,
)
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.admin import JobAdminService, JobCommandContext
from long_invest.platform.jobs.contracts import SubmitPostgresJob
from long_invest.platform.jobs.postgres_service import PostgresJobService

JOB_TYPE = "MARKET_HISTORY_BACKFILL"
QUEUE = "postgres"
LEGACY_QUEUE = "bulk-history"


@dataclass(frozen=True, slots=True)
class HistoryBackfillPage:
    items: tuple[Any, ...]
    page: int
    page_size: int
    total: int


class HistoryBackfillService:
    def __init__(
        self,
        *,
        scope_snapshots: HistoryScopeSnapshotPort,
        jobs: PostgresJobService,
        legacy_jobs: Any | None = None,
        date_ranges: HistoryDateRangePort | None = None,
        admin: JobAdminService,
        audit: Any,
    ) -> None:
        self._scope_snapshots = scope_snapshots
        self._jobs = jobs
        self._legacy_jobs = legacy_jobs
        self._date_ranges = date_ranges
        self._admin = admin
        self._audit = audit

    async def create(
        self,
        session: Any,
        command: CreateHistoryBackfill,
        context: HistoryBackfillAuditContext,
        *,
        owner_user_id: UUID,
    ) -> Any:
        await self._jobs.lock_submission(
            "market-history:backfill", context.idempotency_key
        )
        existing = await self._jobs.find_submission(
            "market-history:backfill", context.idempotency_key
        )
        if existing is not None:
            _require_same_request(existing.config_snapshot, command, context)
            return await self._jobs.submit(
                _submit_command(existing.config_snapshot, context)
            )

        frozen = await self._scope_snapshots.freeze(
            session, command, owner_user_id=owner_user_id
        )
        if command.start_date is None or command.end_date is None:
            if self._date_ranges is None:
                raise AppError(
                    code="HISTORY_DATE_RANGE_NOT_CONFIGURED",
                    message="历史完整区间计算尚未完成生产装配",
                    status_code=503,
                )
            start_date, end_date = await self._date_ranges.complete_range()
            date_mode = "COMPLETE"
        else:
            start_date, end_date = command.start_date, command.end_date
            date_mode = "ADVANCED"
        snapshot = {
            "scope": command.scope.value,
            "requested_symbols": list(command.symbols),
            "requested_watchlist_id": (
                str(command.watchlist_id) if command.watchlist_id else None
            ),
            "universe_snapshot_id": str(frozen.snapshot_id),
            "universe_master_version": frozen.master_version,
            "date_mode": date_mode,
            "requested_start_date": (
                command.start_date.isoformat() if command.start_date else None
            ),
            "requested_end_date": (
                command.end_date.isoformat() if command.end_date else None
            ),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "concurrency": command.concurrency,
            "reason": context.reason,
            "item_count": len(frozen.items),
            "provider_code": (
                command.provider_code.value if command.provider_code else None
            ),
            "source_job_id": (
                str(command.source_job_id) if command.source_job_id else None
            ),
        }
        job = await self._jobs.submit(_submit_command(snapshot, context))
        await self._audit.append(
            AuditWrite(
                action_code="market_history.backfill_created",
                object_type="job",
                object_id=str(job.id),
                result="SUCCESS",
                request_id=context.request_id,
                idempotency_key=_audit_key(context.idempotency_key),
                risk_level="HIGH",
                reason=context.reason,
                before_summary=None,
                after_summary={
                    "scope": command.scope.value,
                    "snapshot_id": str(frozen.snapshot_id),
                    "date_mode": date_mode,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "item_count": len(frozen.items),
                    "concurrency": command.concurrency,
                },
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )
        return job

    async def list(self, *, page: int, page_size: int) -> HistoryBackfillPage:
        result = await self._admin.list_jobs(
            page=page,
            page_size=page_size,
            job_type=JOB_TYPE,
            queue=None,
        )
        return HistoryBackfillPage(
            items=result.items,
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )

    async def get(self, job_id: UUID) -> Any:
        job = await self._admin.get_job(job_id)
        if job.job_type != JOB_TYPE or job.queue not in {QUEUE, LEGACY_QUEUE}:
            raise _not_found()
        return job

    async def command(
        self,
        job_id: UUID,
        action: str,
        context: JobCommandContext,
    ) -> Any:
        job = await self.get(job_id)
        mapped = action
        if action == "retry-failed":
            mapped = "retry" if job.queue == QUEUE else "retry-failed-items"
        if mapped not in {
            "pause",
            "resume",
            "cancel",
            "retry",
            "retry-failed-items",
        }:
            raise ValueError("unsupported history backfill action")
        return await self._admin.command(job_id, mapped, context)

    async def allowed_actions(
        self, job_id: UUID
    ) -> tuple[HistoryBackfillAction, ...]:
        await self.get(job_id)
        actions = await self._admin.allowed_actions(job_id)
        mapped = {
            "pause": HistoryBackfillAction.PAUSE,
            "resume": HistoryBackfillAction.RESUME,
            "cancel": HistoryBackfillAction.CANCEL,
            "retry": HistoryBackfillAction.RETRY_FAILED,
            "retry-failed-items": HistoryBackfillAction.RETRY_FAILED,
        }
        result: list[HistoryBackfillAction] = []
        for action in actions:
            value = mapped.get(action)
            if value is not None and value not in result:
                result.append(value)
        return tuple(result)

    async def allowed_actions_many(
        self, job_ids: tuple[UUID, ...]
    ) -> dict[UUID, tuple[HistoryBackfillAction, ...]]:
        result: dict[UUID, tuple[HistoryBackfillAction, ...]] = {}
        for job_id in job_ids:
            result[job_id] = await self.allowed_actions(job_id)
        return result

    async def item_status_counts_many(
        self, job_ids: tuple[UUID, ...]
    ) -> dict[UUID, dict[str, int]]:
        result: dict[UUID, dict[str, int]] = {}
        legacy_ids: list[UUID] = []
        for job_id in job_ids:
            job = await self.get(job_id)
            if job.queue == LEGACY_QUEUE:
                legacy_ids.append(job_id)
                continue
            total = int(
                (job.progress or {}).get("total")
                or job.config_snapshot.get("item_count", 0)
            )
            completed = int((job.progress or {}).get("completed", 0))
            data = (job.result_summary or {}).get("data", {})
            checkpoint = job.checkpoint or {}
            failed = (
                int(data.get("failed", 0))
                if isinstance(data, dict) and data
                else len(checkpoint.get("failures", ()))
            )
            succeeded = (
                int(data.get("succeeded", 0))
                if isinstance(data, dict) and data
                else int(checkpoint.get("base_succeeded", 0))
                + int(
                    checkpoint.get("succeeded", max(0, completed - failed))
                )
            )
            anomalous = len(checkpoint.get("anomalies", ()))
            pending = max(0, total - completed)
            result[job_id] = {
                "PENDING": pending,
                "RUNNING": 0,
                "SUCCEEDED": max(0, succeeded - anomalous),
                "ANOMALY": anomalous,
                "FAILED": failed,
                "CANCELED": 0,
            }
        if legacy_ids:
            if self._legacy_jobs is None:
                raise RuntimeError("legacy history job reader is not configured")
            result.update(
                await self._legacy_jobs.item_status_counts_many(tuple(legacy_ids))
            )
        return result


def _submit_command(
    snapshot: dict[str, Any], context: HistoryBackfillAuditContext
) -> SubmitPostgresJob:
    return SubmitPostgresJob(
        job_type=JOB_TYPE,
        module_owner="market_data",
        priority=3,
        idempotency_scope="market-history:backfill",
        idempotency_key=context.idempotency_key,
        request_id=context.request_id,
        config_snapshot=snapshot,
        business_object_type="market_history_backfill",
        created_by_user_id=context.actor_user_id,
        soft_timeout_seconds=82800,
        hard_timeout_seconds=86400,
        max_attempts=2,
        recoverable=True,
        max_recoveries=3,
    )


def _audit_key(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return f"market-history-backfill:{digest}"


def _require_same_request(
    snapshot: dict[str, Any],
    command: CreateHistoryBackfill,
    context: HistoryBackfillAuditContext,
) -> None:
    expected = {
        "scope": command.scope.value,
        "requested_symbols": list(command.symbols),
        "requested_watchlist_id": (
            str(command.watchlist_id) if command.watchlist_id else None
        ),
        "requested_start_date": (
            command.start_date.isoformat() if command.start_date else None
        ),
        "requested_end_date": (
            command.end_date.isoformat() if command.end_date else None
        ),
        "concurrency": command.concurrency,
        "provider_code": command.provider_code.value if command.provider_code else None,
        "source_job_id": str(command.source_job_id) if command.source_job_id else None,
        "reason": context.reason,
    }
    if any(snapshot.get(key) != value for key, value in expected.items()):
        raise AppError(
            code="HISTORY_BACKFILL_IDEMPOTENCY_CONFLICT",
            message="相同幂等键不能用于不同的历史回填请求",
            status_code=409,
        )


def _not_found() -> AppError:
    return AppError(
        code="HISTORY_BACKFILL_NOT_FOUND",
        message="历史回填任务不存在",
        status_code=404,
    )
