import os
from datetime import UTC, date, datetime
from decimal import Decimal
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from long_invest.modules.daily_data.contracts import (
    CreateDailyBatch,
    DailyBatchStatus,
    DailyStageStatus,
    StageDailyBar,
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
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
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
