import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import monotonic
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from long_invest.modules.daily_data.contracts import (
    CreateDailyBatch,
    DailyBatchStatus,
    DailyStageStatus,
    StageDailyBar,
)
from long_invest.modules.daily_data.jobs import (
    DailyMarketRecoveryJob,
    FullMarketDailyJob,
)
from long_invest.modules.daily_data.models import (
    DailyBarRevision,
    DailyBarStage,
    DailyBarUnadjusted,
    DailyBatchMissingItem,
    DailyDataBatch,
)
from long_invest.modules.daily_data.outbox import DailyDataEventWriter
from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.modules.daily_data.service import DailyDataService
from long_invest.modules.market_data.models import DataQualityIssue
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import QualityIssueService
from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyCollectionMode,
    DailyCollectionPlan,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
)
from long_invest.modules.securities.models import (
    Security,
    SecurityMasterVersion,
)
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    SubmitPostgresJob,
)
from long_invest.platform.jobs.models import Job, JobItem, JobRun
from long_invest.platform.jobs.postgres_service import PostgresJobService
from long_invest.platform.outbox.models import EventOutbox

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SERVER_PERFORMANCE") != "1",
    reason="server performance acceptance is opt-in",
)


class OneInvalidRowRepository(DailyDataRepository):
    async def store_current_daily_bars(self, rows, revisions_by_key):
        changed = [dict(row) for row in rows]
        changed[len(changed) // 2]["close"] = Decimal("-1")
        return await super().store_current_daily_bars(changed, revisions_by_key)


class SnapshotProviderService:
    def __init__(self, symbols: tuple[str, ...], trading_date: date) -> None:
        self.symbols = symbols
        self.trading_date = trading_date

    async def daily_collection_plan(self, total_symbols: int):
        return DailyCollectionPlan(
            ProviderCode.TUSHARE,
            DailyCollectionMode.SNAPSHOT,
            total_symbols,
            total_symbols,
            0.01,
        )

    async def budget(self, provider):
        return {
            "provider_code": provider.value,
            "daily_limit": 50000,
            "used": 0,
            "remaining": 50000,
        }

    async def market_daily_bars(self, plan, request, deadline):
        del plan, deadline
        return ProviderBatchResult(
            tuple(
                DailyBar(
                    symbol=symbol,
                    trading_date=request.trading_date,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    volume=100,
                    amount=Decimal("1000"),
                    source=ProviderCode.TUSHARE,
                    capability=ProviderCapability.DAILY_BAR_UNADJUSTED,
                    collected_at=datetime(2026, 7, 30, 9, tzinfo=UTC),
                )
                for symbol in request.symbols
            )
        )

    async def daily_bars(self, request, deadline):
        del deadline
        return ProviderBatchResult(
            (
                DailyBar(
                    symbol=request.symbol,
                    trading_date=request.start,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    volume=100,
                    amount=Decimal("1000"),
                    source=ProviderCode.TUSHARE,
                    capability=request.capability,
                    collected_at=datetime(2026, 7, 30, 9, tzinfo=UTC),
                ),
            )
        )


@pytest.mark.anyio
async def test_full_market_daily_5500_rows_use_batched_database_writes() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    trading_date = date(2026, 7, 30)
    now = datetime(2026, 7, 30, 9, tzinfo=UTC)
    symbols = tuple(f"{300000 + index:06d}.SZ" for index in range(5500))
    security_ids = tuple(uuid4() for _ in symbols)
    batch_id = None
    try:
        async with database.transaction() as session:
            service = DailyDataService(
                DailyDataRepository(session), now_provider=lambda: now
            )
            batch = await service.create(
                CreateDailyBatch(
                    trading_date=trading_date,
                    universe_snapshot_id=uuid4(),
                    symbols=symbols,
                    security_ids=security_ids,
                    idempotency_key=f"performance:{uuid4()}",
                    plan_snapshot={
                        "provider": "EASTMONEY",
                        "mode": "PAGED",
                        "total_symbols": len(symbols),
                        "group_size": 100,
                        "estimated_requests": 55,
                        "estimated_seconds": 28,
                    },
                )
            )
            batch_id = batch.id

        stages = tuple(
            StageDailyBar(
                symbol=symbol,
                security_id=security_id,
                trading_date=trading_date,
                status=DailyStageStatus.FETCHED,
                provider_payload={
                    "symbol": symbol,
                    "trading_date": trading_date,
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10.5",
                    "previous_close": "10",
                    "volume": 100,
                    "amount": "1000",
                    "source": "EASTMONEY",
                },
                received_at=now,
            )
            for symbol, security_id in zip(symbols, security_ids, strict=True)
        )
        started = monotonic()
        async with database.transaction() as session:
            await DailyDataService(
                DailyDataRepository(session), now_provider=lambda: now
            ).stage_many(batch_id, stages, requested_count=len(symbols))
        staged_seconds = monotonic() - started

        started = monotonic()
        async with database.transaction() as session:
            service = DailyDataService(
                DailyDataRepository(session),
                events=DailyDataEventWriter(session),
                quality_issues=QualityIssueService(QualityIssueRepository(session)),
                now_provider=lambda: now,
            )
            await service.validate(batch_id)
            result = await service.commit(batch_id)
        committed_seconds = monotonic() - started

        async with database.session() as session:
            stored = int(
                await session.scalar(
                    select(func.count())
                    .select_from(DailyBarUnadjusted)
                    .where(DailyBarUnadjusted.trade_date == trading_date)
                )
                or 0
            )
        assert result.status is DailyBatchStatus.SUCCEEDED
        assert result.committed_count == len(symbols)
        assert stored == len(symbols)
        assert staged_seconds < 30
        assert committed_seconds < 30
        print(
            {
                "rows": len(symbols),
                "stage_seconds": round(staged_seconds, 3),
                "validate_commit_seconds": round(committed_seconds, 3),
            }
        )
    finally:
        if batch_id is not None:
            async with database.transaction() as session:
                await session.execute(
                    delete(EventOutbox).where(
                        EventOutbox.aggregate_id == str(batch_id)
                    )
                )
                await session.execute(
                    delete(DataQualityIssue).where(
                        DataQualityIssue.subject_id == str(batch_id)
                    )
                )
                await session.execute(
                    delete(DailyBarRevision).where(
                        DailyBarRevision.daily_bar_security_id.in_(security_ids)
                    )
                )
                await session.execute(
                    delete(DailyBarUnadjusted).where(
                        DailyBarUnadjusted.security_id.in_(security_ids)
                    )
                )
                await session.execute(
                    delete(DailyBatchMissingItem).where(
                        DailyBatchMissingItem.batch_id == batch_id
                    )
                )
                await session.execute(
                    delete(DailyBarStage).where(DailyBarStage.batch_id == batch_id)
                )
                await session.execute(
                    delete(DailyDataBatch).where(DailyDataBatch.id == batch_id)
                )
        await database.dispose()


@pytest.mark.anyio
async def test_one_database_row_failure_does_not_rollback_the_batch() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    trading_date = date(2026, 7, 30)
    now = datetime(2026, 7, 30, 9, tzinfo=UTC)
    symbols = tuple(f"{310000 + index:06d}.SZ" for index in range(100))
    security_ids = tuple(uuid4() for _ in symbols)
    batch_id = None
    try:
        async with database.transaction() as session:
            service = DailyDataService(
                DailyDataRepository(session), now_provider=lambda: now
            )
            batch = await service.create(
                CreateDailyBatch(
                    trading_date=trading_date,
                    universe_snapshot_id=uuid4(),
                    symbols=symbols,
                    security_ids=security_ids,
                    idempotency_key=f"failure-isolation:{uuid4()}",
                )
            )
            batch_id = batch.id
            await service.stage_many(
                batch.id,
                tuple(
                    StageDailyBar(
                        symbol=symbol,
                        security_id=security_id,
                        trading_date=trading_date,
                        status=DailyStageStatus.FETCHED,
                        provider_payload={
                            "symbol": symbol,
                            "trading_date": trading_date,
                            "open": "10",
                            "high": "11",
                            "low": "9",
                            "close": "10.5",
                            "previous_close": "10",
                            "volume": 100,
                            "amount": "1000",
                            "source": "EASTMONEY",
                        },
                        received_at=now,
                    )
                    for symbol, security_id in zip(
                        symbols, security_ids, strict=True
                    )
                ),
                requested_count=len(symbols),
            )

        async with database.transaction() as session:
            service = DailyDataService(
                OneInvalidRowRepository(session),
                events=DailyDataEventWriter(session),
                quality_issues=QualityIssueService(QualityIssueRepository(session)),
                now_provider=lambda: now,
            )
            await service.validate(batch_id)
            result = await service.commit(batch_id)

        assert result.status is DailyBatchStatus.PARTIAL
        assert result.committed_count == 99
        assert result.failed_count == 1
    finally:
        if batch_id is not None:
            async with database.transaction() as session:
                await session.execute(
                    delete(EventOutbox).where(
                        EventOutbox.aggregate_id == str(batch_id)
                    )
                )
                await session.execute(
                    delete(DataQualityIssue).where(
                        DataQualityIssue.subject_id == str(batch_id)
                    )
                )
                await session.execute(
                    delete(DailyBarUnadjusted).where(
                        DailyBarUnadjusted.security_id.in_(security_ids)
                    )
                )
                await session.execute(
                    delete(DailyBatchMissingItem).where(
                        DailyBatchMissingItem.batch_id == batch_id
                    )
                )
                await session.execute(
                    delete(DailyBarStage).where(DailyBarStage.batch_id == batch_id)
                )
                await session.execute(
                    delete(DailyDataBatch).where(DailyDataBatch.id == batch_id)
                )
        await database.dispose()


@pytest.mark.anyio
async def test_full_market_job_uses_one_job_and_one_batch_with_checkpoint() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    trading_date = date(2026, 7, 30)
    now = datetime(2026, 7, 30, 9, tzinfo=UTC)
    symbols = tuple(f"{320000 + index:06d}.SZ" for index in range(10))
    security_ids = tuple(uuid4() for _ in symbols)
    version_id = uuid4()
    job_id = None
    batch_id = None
    try:
        async with database.transaction() as session:
            session.add(
                SecurityMasterVersion(
                    id=version_id,
                    source="SERVER_TEST",
                    source_version=uuid4().hex,
                    idempotency_key=uuid4().hex,
                    content_hash="a" * 64,
                    master_version=1,
                    item_count=len(symbols),
                )
            )
            session.add_all(
                Security(
                    id=security_id,
                    symbol=symbol,
                    exchange_code=symbol[:6],
                    name=f"test-{index}",
                    market="SZ",
                    security_type="A_SHARE",
                    listed_on=trading_date,
                    listing_status="LISTED",
                    is_st=False,
                    is_suspended=False,
                    provider_codes={},
                    master_version=1,
                    source="SERVER_TEST",
                    source_version="1",
                )
                for index, (symbol, security_id) in enumerate(
                    zip(symbols, security_ids, strict=True)
                )
            )
            submitted = await PostgresJobService(session).submit(
                SubmitPostgresJob(
                    job_type="DAILY_MARKET_DATA",
                    module_owner="market_data",
                    idempotency_scope=f"server-test:{uuid4()}",
                    idempotency_key="daily",
                    request_id=f"server-test-{uuid4()}",
                    config_snapshot={"trade_date": trading_date.isoformat()},
                    recoverable=True,
                ),
                now=now,
            )
            job_id = submitted.id
        async with database.transaction() as session:
            claim = await PostgresJobService(session).claim_next(
                worker_id="server-test",
                lease_duration=timedelta(minutes=1),
                job_types=("DAILY_MARKET_DATA",),
                now=now,
            )
        assert claim is not None

        provider = SnapshotProviderService(symbols, trading_date)
        result = await FullMarketDailyJob(
            database,
            provider_service_factory=lambda _session: provider,
            now_provider=lambda: now,
        )(
            JobExecutionContext(
                job_id=claim.job_id,
                fence_token=claim.lease_token,
                config=claim.config_snapshot,
                checkpoint=claim.checkpoint,
            )
        )
        batch_id = UUID(result.data["batch_id"])

        async with database.session() as session:
            stored_job = await session.get(Job, job_id)
            batch = await session.get(DailyDataBatch, batch_id)
            job_items = int(
                await session.scalar(
                    select(func.count()).select_from(JobItem).where(
                        JobItem.job_id == job_id
                    )
                )
                or 0
            )
            stage_diagnostics = list(
                await session.execute(
                    select(
                        DailyBarStage.symbol,
                        DailyBarStage.status,
                        DailyBarStage.error_code,
                        DailyBarStage.quality_code,
                    ).where(DailyBarStage.batch_id == batch_id)
                )
            )
        assert result.success is True, stage_diagnostics
        assert batch is not None
        assert batch.status == "SUCCEEDED"
        assert batch.committed_count == len(symbols)
        assert stored_job is not None
        assert stored_job.checkpoint["next_group"] == 1
        assert job_items == 0
    finally:
        async with database.transaction() as session:
            if batch_id is not None:
                await session.execute(
                    delete(EventOutbox).where(
                        EventOutbox.aggregate_id.in_((str(batch_id), str(job_id)))
                    )
                )
                await session.execute(
                    delete(DailyBarUnadjusted).where(
                        DailyBarUnadjusted.security_id.in_(security_ids)
                    )
                )
                await session.execute(
                    delete(DailyBatchMissingItem).where(
                        DailyBatchMissingItem.batch_id == batch_id
                    )
                )
                await session.execute(
                    delete(DailyBarStage).where(DailyBarStage.batch_id == batch_id)
                )
                await session.execute(
                    delete(DailyDataBatch).where(DailyDataBatch.id == batch_id)
                )
            if job_id is not None:
                await session.execute(delete(JobRun).where(JobRun.job_id == job_id))
                await session.execute(delete(JobItem).where(JobItem.job_id == job_id))
                await session.execute(delete(Job).where(Job.id == job_id))
        await database.dispose()


@pytest.mark.anyio
async def test_startup_recovery_uses_one_job_for_two_dates_without_job_items() -> None:
    database = Database(AppSettings(_env_file=None).database_owner_url)
    trade_dates = (date(2026, 7, 28), date(2026, 7, 29))
    now = datetime(2026, 7, 30, 1, tzinfo=UTC)
    symbols = tuple(f"{330000 + index:06d}.SZ" for index in range(10))
    security_ids = tuple(uuid4() for _ in symbols)
    version_id = uuid4()
    job_id = None
    batch_ids: tuple[UUID, ...] = ()
    try:
        async with database.transaction() as session:
            session.add(
                SecurityMasterVersion(
                    id=version_id,
                    source="SERVER_RECOVERY_TEST",
                    source_version=uuid4().hex,
                    idempotency_key=uuid4().hex,
                    content_hash="b" * 64,
                    master_version=2,
                    item_count=len(symbols),
                )
            )
            session.add_all(
                Security(
                    id=security_id,
                    symbol=symbol,
                    exchange_code=symbol[:6],
                    name=f"recovery-{index}",
                    market="SZ",
                    security_type="A_SHARE",
                    listed_on=trade_dates[0],
                    listing_status="LISTED",
                    is_st=False,
                    is_suspended=False,
                    provider_codes={},
                    master_version=2,
                    source="SERVER_RECOVERY_TEST",
                    source_version="1",
                )
                for index, (symbol, security_id) in enumerate(
                    zip(symbols, security_ids, strict=True)
                )
            )
            submitted = await PostgresJobService(session).submit(
                SubmitPostgresJob(
                    job_type="DAILY_MARKET_RECOVERY",
                    module_owner="market_data",
                    idempotency_scope=f"server-recovery:{uuid4()}",
                    idempotency_key="two-dates",
                    request_id=f"server-recovery-{uuid4()}",
                    config_snapshot={
                        "trade_dates": [item.isoformat() for item in trade_dates],
                        "concurrency": 4,
                    },
                    recoverable=True,
                ),
                now=now,
            )
            job_id = submitted.id
        async with database.transaction() as session:
            claim = await PostgresJobService(session).claim_next(
                worker_id="server-recovery-test",
                lease_duration=timedelta(minutes=1),
                job_types=("DAILY_MARKET_RECOVERY",),
                now=now,
            )
        assert claim is not None

        provider = SnapshotProviderService(symbols, trade_dates[0])
        handler = DailyMarketRecoveryJob(
            database,
            provider_service_factory=lambda _session: provider,
            now_provider=lambda: now,
        )
        result = await handler(
            JobExecutionContext(
                job_id=claim.job_id,
                fence_token=claim.lease_token,
                config=claim.config_snapshot,
                checkpoint=claim.checkpoint,
            )
        )
        batch_ids = tuple(UUID(item) for item in result.data["batch_ids"])

        async with database.session() as session:
            stored_job = await session.get(Job, job_id)
            batches = list(
                await session.scalars(
                    select(DailyDataBatch)
                    .where(DailyDataBatch.id.in_(batch_ids))
                    .order_by(DailyDataBatch.trading_date)
                )
            )
            stored_rows = int(
                await session.scalar(
                    select(func.count())
                    .select_from(DailyBarUnadjusted)
                    .where(DailyBarUnadjusted.security_id.in_(security_ids))
                )
                or 0
            )
            job_items = int(
                await session.scalar(
                    select(func.count()).select_from(JobItem).where(
                        JobItem.job_id == job_id
                    )
                )
                or 0
            )
            diagnostics = list(
                await session.execute(
                    select(
                        DailyBarStage.symbol,
                        DailyBarStage.trading_date,
                        DailyBarStage.status,
                        DailyBarStage.error_code,
                        DailyBarStage.quality_code,
                    ).where(DailyBarStage.batch_id.in_(batch_ids))
                )
            )
        resumed = await handler(
            JobExecutionContext(
                job_id=claim.job_id,
                fence_token=claim.lease_token,
                config=claim.config_snapshot,
                checkpoint=stored_job.checkpoint,
            )
        )
        assert result.success is True
        assert resumed.data["batch_ids"] == result.data["batch_ids"]
        assert [item.trading_date for item in batches] == list(trade_dates)
        assert all(item.status == "SUCCEEDED" for item in batches), {
            "batches": [
                {
                    "date": item.trading_date.isoformat(),
                    "status": item.status,
                    "committed": item.committed_count,
                    "missing": item.missing_count,
                    "failed": item.failed_count,
                }
                for item in batches
            ],
            "stages": diagnostics,
        }
        assert stored_rows == len(symbols) * len(trade_dates)
        assert stored_job is not None
        assert stored_job.checkpoint["date_index"] == len(trade_dates)
        assert job_items == 0
    finally:
        async with database.transaction() as session:
            if batch_ids:
                await session.execute(
                    delete(EventOutbox).where(
                        EventOutbox.aggregate_id.in_(
                            tuple(str(item) for item in batch_ids)
                        )
                    )
                )
                await session.execute(
                    delete(DailyBarUnadjusted).where(
                        DailyBarUnadjusted.security_id.in_(security_ids)
                    )
                )
                await session.execute(
                    delete(DailyBatchMissingItem).where(
                        DailyBatchMissingItem.batch_id.in_(batch_ids)
                    )
                )
                await session.execute(
                    delete(DailyBarStage).where(
                        DailyBarStage.batch_id.in_(batch_ids)
                    )
                )
                await session.execute(
                    delete(DailyDataBatch).where(DailyDataBatch.id.in_(batch_ids))
                )
            if job_id is not None:
                await session.execute(delete(JobRun).where(JobRun.job_id == job_id))
                await session.execute(delete(JobItem).where(JobItem.job_id == job_id))
                await session.execute(delete(Job).where(Job.id == job_id))
        await database.dispose()
