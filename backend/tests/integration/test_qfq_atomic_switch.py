from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.modules.qfq.contracts import QfqBarInput, ValidatedQfqWindow
from long_invest.modules.qfq.models import QfqDataset, QfqRefreshRun
from long_invest.modules.qfq.outbox import QfqEventAdapter
from long_invest.modules.qfq.repository import QfqRepository
from long_invest.modules.qfq.service import QfqRefreshService
from long_invest.modules.securities.models import Security
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.models import Job
from long_invest.platform.outbox.models import EventOutbox

pytestmark = pytest.mark.skipif(
    os.environ.get("LONGINVEST_QFQ_POSTGRES_TESTS") != "1",
    reason="requires the migrated PostgreSQL test profile",
)

NOW = datetime(2026, 7, 16, 10, tzinfo=UTC)
START = date(2026, 7, 15)
END = date(2026, 7, 16)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _window(checksum: str) -> ValidatedQfqWindow:
    bars = tuple(
        QfqBarInput(
            trade_date=trade_date,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=100,
            amount=Decimal("1050"),
        )
        for trade_date in (START, END)
    )
    return ValidatedQfqWindow(
        bars=bars,
        anchor_date=END,
        anchor_close=Decimal("10.5"),
        row_count=2,
        checksum=checksum,
    )


async def _seed(database: Database, run_count: int = 1):
    token = uuid4().hex
    symbol = f"{int(token[:8], 16) % 1_000_000:06d}.SH"
    security = Security(
        id=uuid4(),
        symbol=symbol,
        exchange_code=symbol[:6],
        name=f"QFQ integration {token}",
        market="SH",
        security_type="A_SHARE",
        listing_status="LISTED",
        is_st=False,
        is_suspended=False,
        provider_codes={},
        master_version=1,
        source="integration-test",
        source_version=token,
        updated_at=NOW,
    )
    jobs = []
    runs = []
    for index in range(run_count):
        job = Job(
            id=uuid4(),
            job_type="QFQ_REFRESH",
            queue="qfq-refresh",
            priority=0,
            status="RUNNING",
            config_snapshot={},
            idempotency_scope=f"qfq-test:{token}",
            idempotency_key=f"key-{index}",
            request_hash=f"{index + 1:064x}",
            request_id=f"req-{token}-{index}",
            soft_timeout_seconds=240,
            hard_timeout_seconds=300,
            progress={},
            version=1,
        )
        run = QfqRefreshRun(
            id=uuid4(),
            job_id=job.id,
            security_id=security.id,
            symbol=symbol,
            requested_start=START,
            requested_end=END,
            as_of_date=END,
            expected_trade_dates=[START.isoformat(), END.isoformat()],
            input_daily_version=3,
            trigger_reason="MANUAL",
            request_id=job.request_id,
            idempotency_key=job.idempotency_key,
            request_hash=job.request_hash,
            status="VALIDATING",
            provider="eastmoney",
            created_at=NOW,
            updated_at=NOW,
        )
        jobs.append(job)
        runs.append(run)
    async with database.transaction() as session:
        session.add(security)
        session.add_all(jobs)
        await session.flush()
        session.add_all(runs)
    return security, runs


class FailingWriter:
    async def append(self, **_kwargs):
        raise RuntimeError("outbox failed")


@pytest.mark.anyio
async def test_outbox_failure_rolls_back_dataset_run_and_event() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    security, (run,) = await _seed(database)
    try:
        with pytest.raises(RuntimeError, match="outbox failed"):
            async with database.transaction() as session:
                repository = QfqRepository(session)
                service = QfqRefreshService(
                    repository,
                    events=QfqEventAdapter(session, FailingWriter()),
                )
                await service.activate(
                    run.id,
                    _window("a" * 64),
                    current_input_daily_version=3,
                    provider_contract_version="eastmoney-v1",
                    now=NOW,
                )

        async with database.session() as session:
            stored_run = await session.get(QfqRefreshRun, run.id)
            dataset_count = await session.scalar(
                select(func.count())
                .select_from(QfqDataset)
                .where(QfqDataset.security_id == security.id)
            )
            event_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.aggregate_id == str(run.id))
            )
        assert stored_run is not None and stored_run.status == "VALIDATING"
        assert dataset_count == 0
        assert event_count == 0
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_concurrent_activations_leave_exactly_one_current_dataset() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    security, runs = await _seed(database, run_count=2)

    async def activate(run, checksum):
        async with database.transaction() as session:
            repository = QfqRepository(session)
            service = QfqRefreshService(
                repository,
                events=QfqEventAdapter(session),
            )
            return await service.activate(
                run.id,
                _window(checksum),
                current_input_daily_version=3,
                provider_contract_version="eastmoney-v1",
                now=NOW,
            )

    try:
        await asyncio.gather(
            activate(runs[0], "a" * 64),
            activate(runs[1], "b" * 64),
        )
        async with database.session() as session:
            datasets = list(
                await session.scalars(
                    select(QfqDataset)
                    .where(QfqDataset.security_id == security.id)
                    .order_by(QfqDataset.version)
                )
            )
        assert [dataset.version for dataset in datasets] == [1, 2]
        assert sum(dataset.lifecycle == "CURRENT" for dataset in datasets) == 1
        assert sum(dataset.lifecycle == "SUPERSEDED" for dataset in datasets) == 1
    finally:
        await database.dispose()
