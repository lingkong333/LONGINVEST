from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.bootstrap.providers import build_provider_service
from long_invest.modules.calendar.application import CalendarApplication
from long_invest.modules.daily_data.contracts import HistoricalDailyBarInput
from long_invest.modules.daily_data.outbox import DailyDataEventWriter
from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.modules.daily_data.service import DailyDataService
from long_invest.modules.history_backfills.application import (
    HistoryBackfillApplication,
)
from long_invest.modules.history_backfills.contracts import (
    HistoryBackfillItemError,
    HistoryBackfillWorkItem,
    HistoryBarInput,
    HistoryBarsBundle,
    HistoryBarStoreResult,
)
from long_invest.modules.history_backfills.integrations import (
    SecurityHistoryScopeSnapshotAdapter,
)
from long_invest.modules.history_backfills.jobs import (
    build_postgres_history_backfill_handler,
)
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import QualityIssueService
from long_invest.modules.providers.contracts import (
    DailyBarRequest,
    ProviderCapability,
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


class CalendarHistoryDateRange:
    def __init__(self, database: Database) -> None:
        self._calendar = CalendarApplication(database)

    async def complete_range(self) -> tuple[date, date]:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        completed_before = (
            now.date() + timedelta(days=1)
            if now.time() >= time(17)
            else now.date()
        )
        end_date = await self._calendar.latest_completed_trading_date(
            completed_before
        )
        return date(1990, 12, 19), end_date


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
        concurrency: int,
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
                    concurrency=concurrency,
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
                    concurrency=concurrency,
                )
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
            provider_contract_version=_provider_contract_version(
                unadjusted.items, qfq.items
            ),
        )


def _require_provider_result(result) -> None:
    if result.batch_error_code:
        raise HistoryBackfillItemError(result.batch_error_code, retryable=True)
    if result.failures:
        raise HistoryBackfillItemError(result.failures[0].code, retryable=True)


async def _wait_for_history_capacity(
    provider,
    request,
    *,
    deadline: datetime,
    concurrency: int,
):
    failed_attempts = 0
    while True:
        try:
            return await provider.daily_bars(
                request,
                deadline,
                concurrency=concurrency,
            )
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
            source_identity=(
                {
                    "adapter": bar.source_identity.adapter.value,
                    "upstream": bar.source_identity.upstream.value,
                    "interface": bar.source_identity.interface,
                    "capability": bar.source_identity.capability.value,
                    "algorithm_version": bar.source_identity.algorithm_version,
                }
                if bar.source_identity is not None
                else None
            ),
            collected_at=bar.collected_at,
        )
        for bar in bars
    )


def _provider_contract_version(unadjusted, qfq) -> str:
    versions = []
    for bars in (unadjusted, qfq):
        if not bars:
            continue
        bar = bars[0]
        identity = bar.source_identity
        if identity is None:
            versions.append(f"{bar.source.value}:{bar.capability.value}")
        else:
            versions.append(
                ":".join(
                    (
                        identity.adapter.value,
                        identity.upstream.value,
                        identity.interface,
                        identity.capability.value,
                        identity.algorithm_version,
                    )
                )
            )
    return "|".join(versions)


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
                source_identity=bar.source_identity,
                collected_at=bar.collected_at,
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


class FilesystemHistoryDiskGuard:
    async def is_backfill_safe(self) -> bool:
        usage = shutil.disk_usage("/")
        return usage.used / usage.total < 0.95


def build_history_backfill_application() -> HistoryBackfillApplication:
    database = get_database()
    return HistoryBackfillApplication(
        database,
        scope_snapshots=SecurityHistoryScopeSnapshotAdapter(
            watchlists=TransactionWatchlistSymbols()
        ),
        date_ranges=CalendarHistoryDateRange(database),
    )


def build_history_backfill_job_handler(database: Database | None = None):
    database = database or get_database()
    return build_postgres_history_backfill_handler(
        database,
        provider_factory=lambda: DatabaseHistoryBarsProvider(database),
        store_factory=lambda: DatabaseHistoryBarStore(database),
        disk_guard_factory=FilesystemHistoryDiskGuard,
    )
