from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    HistoryBackfillAuditContext,
    HistoryScopeSnapshotPort,
)
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.admin import JobAdminService, JobCommandContext
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService

JOB_TYPE = "MARKET_HISTORY_BACKFILL"
QUEUE = "bulk-history"


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
        jobs: JobService,
        admin: JobAdminService,
        audit: Any,
    ) -> None:
        self._scope_snapshots = scope_snapshots
        self._jobs = jobs
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
        snapshot = {
            "scope": command.scope.value,
            "requested_symbols": list(command.symbols),
            "requested_watchlist_id": (
                str(command.watchlist_id) if command.watchlist_id else None
            ),
            "universe_snapshot_id": str(frozen.snapshot_id),
            "universe_master_version": frozen.master_version,
            "start_date": command.start_date.isoformat(),
            "end_date": command.end_date.isoformat(),
            "concurrency": command.concurrency,
            "reason": context.reason,
            "items": [
                {"security_id": str(item.security_id), "symbol": item.symbol}
                for item in frozen.items
            ],
        }
        job = await self._jobs.submit(_submit_command(snapshot, context))
        await self._jobs.initialize_items(
            job.id, tuple(item.symbol for item in frozen.items)
        )
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
                    "start_date": command.start_date.isoformat(),
                    "end_date": command.end_date.isoformat(),
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
            queue=QUEUE,
        )
        return HistoryBackfillPage(
            items=result.items,
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )

    async def get(self, job_id: UUID) -> Any:
        job = await self._admin.get_job(job_id)
        if job.job_type != JOB_TYPE or job.queue != QUEUE:
            raise _not_found()
        return job

    async def command(
        self,
        job_id: UUID,
        action: str,
        context: JobCommandContext,
    ) -> Any:
        await self.get(job_id)
        mapped = "retry-failed-items" if action == "retry-failed" else action
        if mapped not in {"pause", "resume", "cancel", "retry-failed-items"}:
            raise ValueError("unsupported history backfill action")
        return await self._admin.command(job_id, mapped, context)


def _submit_command(
    snapshot: dict[str, Any], context: HistoryBackfillAuditContext
) -> SubmitJob:
    return SubmitJob(
        job_type=JOB_TYPE,
        queue=QUEUE,
        idempotency_scope="market-history:backfill",
        idempotency_key=context.idempotency_key,
        request_id=context.request_id,
        config_snapshot=snapshot,
        business_object_type="market_history_backfill",
        created_by_user_id=context.actor_user_id,
        soft_timeout_seconds=82800,
        hard_timeout_seconds=86400,
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
        "start_date": command.start_date.isoformat(),
        "end_date": command.end_date.isoformat(),
        "concurrency": command.concurrency,
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
