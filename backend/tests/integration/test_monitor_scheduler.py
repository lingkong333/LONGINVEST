import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

import long_invest.bootstrap.jobs as jobs_module
from long_invest.bootstrap.jobs import realtime_quote_cycle
from long_invest.modules.monitor_schedules.models import (
    MonitorSchedule,
    MonitorScheduleRevision,
)
from long_invest.modules.monitoring.contracts import FrozenSubscription
from long_invest.modules.monitoring.models import ScheduleOccurrence
from long_invest.modules.monitoring.scheduler import (
    MonitorScanner,
    OccurrenceEventAdapter,
    PlannedBatch,
    PlannedOccurrence,
)
from long_invest.modules.securities.models import (
    Security,
    SecurityMasterVersion,
    SecurityUniverseSnapshot,
)
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import JobExecutionContext
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.service import JobService

pytestmark = pytest.mark.skipif(
    os.environ.get("LONGINVEST_MONITOR_SCHEDULER_POSTGRES_TESTS") != "1",
    reason="requires migrated PostgreSQL",
)
NOW = datetime(2026, 7, 17, 2, 15, tzinfo=UTC)


async def _seed(db):
    token = uuid4().hex
    master_version = int(token[:8], 16) % 1_000_000_000 + 1
    symbol = f"{int(token[:8], 16) % 1_000_000:06d}.SH"
    schedule = MonitorSchedule(id=uuid4(), name="integration", version=1)
    security_id = uuid4()
    async with db.transaction() as session:
        session.add(
            SecurityMasterVersion(
                id=uuid4(),
                source="test",
                source_version=token,
                idempotency_key=token,
                content_hash=token * 2,
                master_version=master_version,
                item_count=1,
                result_summary={},
            )
        )
        session.add(
            Security(
                id=security_id,
                symbol=symbol,
                exchange_code=symbol[:6],
                name="scheduler",
                market="SH",
                security_type="A_SHARE",
                listing_status="LISTED",
                is_st=False,
                is_suspended=False,
                provider_codes={},
                master_version=master_version,
                source="test",
                source_version=token,
                updated_at=NOW,
            )
        )
        session.add(schedule)
        await session.flush()
        revision = MonitorScheduleRevision(
            id=uuid4(),
            schedule_id=schedule.id,
            revision_no=1,
            times=["10:15"],
            timezone="Asia/Shanghai",
            reason="test",
            created_by_user_id="test",
            request_id=uuid4().hex,
            idempotency_key=uuid4().hex,
            content_hash="a" * 64,
            metadata_snapshot={},
        )
        session.add(revision)
        await session.flush()
        await session.execute(
            update(MonitorSchedule)
            .where(MonitorSchedule.id == schedule.id)
            .values(current_revision_id=revision.id)
        )
    return (
        schedule.id,
        revision.id,
        FrozenSubscription(
            subscription_id=uuid4(),
            security_id=security_id,
            symbol=symbol,
            version=1,
            revision_id=uuid4(),
        ),
    )


def _scanner(db, events=OccurrenceEventAdapter, jobs=JobService):
    from long_invest.modules.securities.application import SecurityApplication

    security = SecurityApplication(db)

    async def freezer(session, symbols):
        return await security.freeze_symbols_in_transaction(session, symbols)

    return MonitorScanner(
        db,
        None,
        None,
        None,
        job_factory=jobs,
        event_factory=events,
        universe_freezer=freezer,
    )


@pytest.mark.anyio
async def test_duplicate_scanners_create_one_occurrence_and_job() -> None:
    db = Database(AppSettings(_env_file=None).database_url)
    schedule, revision, frozen = await _seed(db)
    schedule2, revision2, frozen2 = await _seed(db)
    batch = PlannedBatch(
        NOW,
        (
            PlannedOccurrence(schedule, revision, NOW, (frozen,)),
            PlannedOccurrence(schedule2, revision2, NOW, (frozen2,)),
        ),
    )
    try:
        async with db.session() as session:
            universe_before = await session.scalar(
                select(func.count()).select_from(SecurityUniverseSnapshot)
            )
        results = await asyncio.gather(
            _scanner(db).claim_batch(batch, now=NOW),
            _scanner(db).claim_batch(batch, now=NOW),
        )
        async with db.session() as session:
            occurrences = await session.scalar(
                select(func.count())
                .select_from(ScheduleOccurrence)
                .where(ScheduleOccurrence.schedule_id.in_((schedule, schedule2)))
            )
            jobs = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.business_object_type == "schedule_occurrence_batch",
                    Job.business_object_id == NOW.isoformat(),
                )
            )
            universe_after = await session.scalar(
                select(func.count()).select_from(SecurityUniverseSnapshot)
            )
        assert {x.status for x in results} == {"DISPATCHED", "DUPLICATE"}
        assert occurrences == 2 and jobs == 1
        assert universe_after == universe_before + 1
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_restart_after_grace_is_missed_without_job() -> None:
    db = Database(AppSettings(_env_file=None).database_url)
    schedule, revision, frozen = await _seed(db)
    plan = PlannedOccurrence(schedule, revision, NOW, (frozen,))
    try:
        async with db.session() as session:
            jobs_before = await session.scalar(select(func.count()).select_from(Job))
        result = await _scanner(db).claim(plan, now=NOW.replace(minute=17))
        assert result.status == "MISSED"
        async with db.session() as session:
            jobs_after = await session.scalar(select(func.count()).select_from(Job))
            occurrence = await session.scalar(
                select(ScheduleOccurrence).where(
                    ScheduleOccurrence.schedule_id == schedule
                )
            )
        assert jobs_after == jobs_before
        assert occurrence is not None and occurrence.job_id is None
    finally:
        await db.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["job", "outbox"])
async def test_job_and_outbox_failure_roll_back_occurrence(failure) -> None:
    class BadJobs:
        def __init__(self, session):
            pass

        async def submit(self, command):
            raise RuntimeError("job failed")

    class BadEvents:
        def __init__(self, session):
            pass

        async def append(self, item, action):
            raise RuntimeError("outbox failed")

    db = Database(AppSettings(_env_file=None).database_url)
    schedule, revision, frozen = await _seed(db)
    scheduled = NOW + timedelta(minutes=1 if failure == "job" else 2)
    plan = PlannedOccurrence(schedule, revision, scheduled, (frozen,))
    try:
        async with db.session() as session:
            universe_before = await session.scalar(
                select(func.count()).select_from(SecurityUniverseSnapshot)
            )
        with pytest.raises(RuntimeError, match=failure):
            await _scanner(
                db,
                jobs=BadJobs if failure == "job" else JobService,
                events=BadEvents if failure == "outbox" else OccurrenceEventAdapter,
            ).claim(plan, now=scheduled)
        async with db.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ScheduleOccurrence)
                .where(ScheduleOccurrence.schedule_id == schedule)
            )
            universe_after = await session.scalar(
                select(func.count()).select_from(SecurityUniverseSnapshot)
            )
        assert count == 0
        assert universe_after == universe_before
    finally:
        await db.dispose()


@pytest.mark.anyio
async def test_worker_claim_after_deadline_marks_occurrence_missed(monkeypatch) -> None:
    db = Database(AppSettings(_env_file=None).database_url)
    schedule, revision, frozen = await _seed(db)
    scheduled = NOW + timedelta(minutes=3)
    try:
        claimed = await _scanner(db).claim(
            PlannedOccurrence(schedule, revision, scheduled, (frozen,)),
            now=scheduled,
        )
        async with db.session() as session:
            job = await session.get(Job, claimed.job_id)
            assert job is not None

        monkeypatch.setattr(
            jobs_module,
            "_utc_now",
            lambda: scheduled + timedelta(seconds=61),
        )
        result = await realtime_quote_cycle(
            JobExecutionContext(
                job_id=job.id,
                fence_token=uuid4(),
                config=job.config_snapshot,
            )
        )

        async with db.session() as session:
            occurrence = await session.scalar(
                select(ScheduleOccurrence).where(ScheduleOccurrence.job_id == job.id)
            )
        assert result.code == "SCHEDULE_OCCURRENCE_MISSED"
        assert occurrence is not None
        assert str(occurrence.status) == "MISSED"
        assert occurrence.error_code == "SCHEDULE_OCCURRENCE_MISSED"
    finally:
        await db.dispose()
