from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.bootstrap.providers import build_provider_service
from long_invest.modules.daily_data.contracts import HistoricalDailyBarInput
from long_invest.modules.daily_data.outbox import DailyDataEventWriter
from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.modules.daily_data.service import DailyDataService
from long_invest.modules.history_backfills.application import (
    HistoryBackfillApplication,
)
from long_invest.modules.history_backfills.contracts import (
    HistoryBackfillControl,
    HistoryBackfillItemError,
    HistoryBackfillWorkItem,
    HistoryBarInput,
    HistoryBarsBundle,
    HistoryBarStoreResult,
    HistoryJobFence,
    HistoryJobItemSummary,
)
from long_invest.modules.history_backfills.integrations import (
    SecurityHistoryScopeSnapshotAdapter,
)
from long_invest.modules.history_backfills.jobs import build_history_backfill_handler
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import QualityIssueService
from long_invest.modules.providers.contracts import (
    DailyBarRequest,
    ProviderCapability,
    ProviderCode,
)
from long_invest.modules.providers.resilience import ProviderCallError
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.modules.qfq.application import get_qfq_application
from long_invest.modules.qfq.contracts import QfqBarInput, RefreshQfq
from long_invest.modules.qfq.validation import validate_qfq_window
from long_invest.modules.watchlists.outbox import WatchlistEventAdapter
from long_invest.modules.watchlists.repository import WatchlistRepository
from long_invest.modules.watchlists.service import WatchlistService
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import JobItemStatus, JobProgress, JobStatus
from long_invest.platform.jobs.service import JobService


class TransactionWatchlistSymbols:
    async def symbols(
        self,
        session: AsyncSession,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
    ) -> tuple[str, ...]:
        view = await WatchlistService(
            WatchlistRepository(session),
            AuditService(session),
            WatchlistEventAdapter(session),
        ).get(watchlist_id, owner_user_id=owner_user_id)
        return tuple(item.symbol for item in view.items)


class DatabaseHistoryBarsProvider:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def fetch(
        self,
        item: HistoryBackfillWorkItem,
        *,
        start_date: date,
        end_date: date,
        deadline: datetime,
    ) -> HistoryBarsBundle:
        try:
            async with self._database.session() as session:
                provider = build_provider_service(session)
                unadjusted = await _wait_for_history_capacity(
                    provider,
                    DailyBarRequest(
                        symbol=item.symbol,
                        start=start_date,
                        end=end_date,
                        capability=ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
                    ),
                    deadline=deadline,
                )
                qfq = await _wait_for_history_capacity(
                    provider,
                    DailyBarRequest(
                        symbol=item.symbol,
                        start=start_date,
                        end=end_date,
                        capability=ProviderCapability.HISTORICAL_DAILY_QFQ,
                    ),
                    deadline=deadline,
                )
                summary = await provider.get_provider(ProviderCode.SINA)
        except ProviderHttpError as error:
            raise HistoryBackfillItemError(
                error.code, retryable=error.retryable
            ) from error
        except ProviderCallError as error:
            raise HistoryBackfillItemError(error.code, retryable=True) from error
        _require_provider_result(unadjusted)
        _require_provider_result(qfq)
        return HistoryBarsBundle(
            unadjusted=_history_inputs(unadjusted.items),
            qfq=_history_inputs(qfq.items),
            provider_contract_version=f"SINA:config-v{int(summary['version'])}",
        )


def _require_provider_result(result) -> None:
    if result.batch_error_code:
        raise HistoryBackfillItemError(result.batch_error_code, retryable=True)
    if result.failures:
        raise HistoryBackfillItemError(result.failures[0].code, retryable=True)


async def _wait_for_history_capacity(provider, request, *, deadline: datetime):
    failed_attempts = 0
    while True:
        try:
            return await provider.daily_bars(request, deadline)
        except ProviderCallError as error:
            if error.code not in {
                "PROVIDER_RATE_LIMITED",
                "PROVIDER_CIRCUIT_OPEN",
            }:
                raise
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise TimeoutError from error
            await asyncio.sleep(min(0.25, remaining))
        except ProviderHttpError as error:
            failed_attempts += 1
            if not error.retryable or failed_attempts >= 3:
                raise
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise TimeoutError from error
            await asyncio.sleep(min(0.25 * 2 ** (failed_attempts - 1), remaining))


def _history_inputs(bars) -> tuple[HistoryBarInput, ...]:
    return tuple(
        HistoryBarInput(
            symbol=bar.symbol,
            trade_date=bar.trading_date,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            amount=bar.amount,
            source=bar.source.value,
        )
        for bar in bars
    )


class DatabaseHistoryBarStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def store(
        self,
        item: HistoryBackfillWorkItem,
        bars: HistoryBarsBundle,
        *,
        idempotency_key: str,
        reason: str,
    ) -> HistoryBarStoreResult:
        if not idempotency_key.strip():
            raise ValueError("history store idempotency key is required")
        inputs = tuple(
            HistoricalDailyBarInput(
                security_id=item.security_id,
                symbol=bar.symbol,
                trade_date=bar.trade_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                amount=bar.amount,
                source=bar.source,
            )
            for bar in bars.unadjusted
        )
        async with self._database.transaction() as session:
            stored = await DailyDataService(
                DailyDataRepository(session),
                events=DailyDataEventWriter(session),
                quality_issues=QualityIssueService(QualityIssueRepository(session)),
            ).store_historical_bars(inputs, reason=reason)
        qfq_dates = tuple(bar.trade_date for bar in bars.qfq)
        raw_by_date = {bar.trade_date: bar for bar in bars.unadjusted}
        anchor = raw_by_date[bars.qfq[-1].trade_date]
        command = RefreshQfq(
            security_id=item.security_id,
            symbol=item.symbol,
            start=bars.qfq[0].trade_date,
            end=bars.qfq[-1].trade_date,
            as_of_date=bars.qfq[-1].trade_date,
            expected_trade_dates=qfq_dates,
            input_daily_version=1,
            trigger_reason=reason,
            request_id=idempotency_key,
            idempotency_key=idempotency_key,
            actor_user_id="history-backfill-worker",
        )
        validated = validate_qfq_window(
            command,
            (
                QfqBarInput(
                    trade_date=bar.trade_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    amount=bar.amount,
                )
                for bar in bars.qfq
            ),
            anchor.close,
        )
        qfq = await get_qfq_application().store_history(
            security_id=item.security_id,
            symbol=item.symbol,
            requested_start=bars.unadjusted[0].trade_date,
            validated_window=validated,
            provider=bars.qfq[0].source,
            provider_contract_version=bars.provider_contract_version,
            reason=reason,
        )
        return HistoryBarStoreResult(
            inserted=stored.inserted,
            unchanged=stored.unchanged,
            revised=stored.revised,
            review_required=stored.review_required,
            qfq_dataset_id=qfq.dataset_id,
            qfq_version=qfq.version,
            qfq_rows=qfq.row_count,
            qfq_unchanged=qfq.unchanged,
            qfq_actual_start=qfq.actual_start,
            qfq_actual_end=qfq.actual_end,
            qfq_truncated_rows=len(bars.unadjusted) - len(bars.qfq),
        )


class DatabaseHistoryJobItems:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def recover_incomplete(self, fence: HistoryJobFence) -> None:
        async with self._database.transaction() as session:
            await self._require(
                await JobService(session).recover_active_items(
                    fence.job_id, fence.fence_token
                )
            )

    async def control(self, fence: HistoryJobFence) -> HistoryBackfillControl:
        async with self._database.transaction() as session:
            status = await JobService(session).control_status(
                fence.job_id, fence.fence_token
            )
        if status in {JobStatus.PAUSING, JobStatus.PAUSED}:
            return HistoryBackfillControl.PAUSE_REQUESTED
        if status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELED}:
            return HistoryBackfillControl.CANCEL_REQUESTED
        if status is None:
            raise self._stale_fence()
        return HistoryBackfillControl.RUNNING

    async def claim_pending(
        self, fence: HistoryJobFence, *, limit: int
    ) -> tuple[HistoryBackfillWorkItem, ...]:
        async with self._database.transaction() as session:
            service = JobService(session)
            keys = await service.claim_pending_items(
                fence.job_id, fence.fence_token, limit=limit
            )
            job = await service.get(fence.job_id)
            if job is None:
                raise self._stale_fence()
            securities = {
                str(value["symbol"]): UUID(str(value["security_id"]))
                for value in job.config_snapshot.get("items", ())
            }
        return tuple(
            HistoryBackfillWorkItem(security_id=securities[key], symbol=key)
            for key in keys
        )

    async def mark_stage(
        self, fence: HistoryJobFence, symbol: str, status: JobItemStatus
    ) -> None:
        async with self._database.transaction() as session:
            await self._require(
                await JobService(session).set_item_stage(
                    fence.job_id, fence.fence_token, symbol, status
                )
            )

    async def release_pending(self, fence: HistoryJobFence, symbol: str) -> None:
        async with self._database.transaction() as session:
            await self._require(
                await JobService(session).release_item(
                    fence.job_id, fence.fence_token, symbol
                )
            )

    async def finish(
        self,
        fence: HistoryJobFence,
        symbol: str,
        *,
        status: JobItemStatus,
        result_ref: dict | None = None,
        error_code: str | None = None,
    ) -> None:
        async with self._database.transaction() as session:
            await self._require(
                await JobService(session).finish_claimed_item(
                    fence.job_id,
                    fence.fence_token,
                    symbol,
                    status=status,
                    result_ref=result_ref,
                    error_code=error_code,
                )
            )

    async def summary(self, fence: HistoryJobFence) -> HistoryJobItemSummary:
        async with self._database.transaction() as session:
            counts = await JobService(session).item_status_counts(
                fence.job_id, fence.fence_token
            )
        if counts is None:
            raise self._stale_fence()
        active = sum(
            counts.get(status.value, 0)
            for status in (
                JobItemStatus.FETCHING,
                JobItemStatus.VALIDATING,
                JobItemStatus.RUNNING,
                JobItemStatus.SAVING,
            )
        )
        return HistoryJobItemSummary(
            total=sum(counts.values()),
            pending=counts.get(JobItemStatus.PENDING.value, 0),
            active=active,
            succeeded=counts.get(JobItemStatus.SUCCEEDED.value, 0)
            + counts.get(JobItemStatus.SKIPPED.value, 0),
            failed=counts.get(JobItemStatus.FAILED.value, 0),
            canceled=counts.get(JobItemStatus.CANCELED.value, 0),
        )

    async def report_progress(
        self, fence: HistoryJobFence, summary: HistoryJobItemSummary
    ) -> None:
        completed = summary.succeeded + summary.failed + summary.canceled
        async with self._database.transaction() as session:
            await self._require(
                await JobService(session).report_progress(
                    job_id=fence.job_id,
                    fence_token=fence.fence_token,
                    progress=JobProgress(completed=completed, total=summary.total),
                )
            )

    async def request_pause(self, fence: HistoryJobFence, *, reason: str) -> None:
        del reason
        async with self._database.transaction() as session:
            await self._require(
                await JobService(session).request_pause_from_worker(
                    fence.job_id, fence.fence_token
                )
            )

    async def cancel_pending(self, fence: HistoryJobFence) -> None:
        async with self._database.transaction() as session:
            await self._require(
                await JobService(session).cancel_pending_items(
                    fence.job_id, fence.fence_token
                )
            )

    @staticmethod
    async def _require(accepted: bool) -> None:
        if not accepted:
            raise DatabaseHistoryJobItems._stale_fence()

    @staticmethod
    def _stale_fence() -> AppError:
        return AppError(
            code="JOB_FENCE_STALE",
            message="历史回填执行权已失效",
            status_code=409,
        )


class FilesystemHistoryDiskGuard:
    async def is_backfill_safe(self) -> bool:
        usage = shutil.disk_usage("/")
        return usage.used / usage.total < 0.95


def build_history_backfill_application() -> HistoryBackfillApplication:
    return HistoryBackfillApplication(
        get_database(),
        scope_snapshots=SecurityHistoryScopeSnapshotAdapter(
            watchlists=TransactionWatchlistSymbols()
        ),
    )


def build_history_backfill_job_handler():
    database = get_database()
    return build_history_backfill_handler(
        provider_factory=lambda: DatabaseHistoryBarsProvider(database),
        store_factory=lambda: DatabaseHistoryBarStore(database),
        items_factory=lambda: DatabaseHistoryJobItems(database),
        disk_guard_factory=FilesystemHistoryDiskGuard,
    )
