from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from long_invest.modules.monitoring.contracts import (
    FrozenSubscription,
    OccurrenceStatus,
    ScheduleOccurrencePageView,
    ScheduleOccurrenceView,
)
from long_invest.modules.monitoring.models import ScheduleOccurrence
from long_invest.platform.database.engine import get_database
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.outbox.service import TransactionalOutboxWriter


@dataclass(frozen=True, slots=True)
class PlannedOccurrence:
    schedule_id: UUID
    schedule_revision_id: UUID
    scheduled_at: datetime
    subscriptions: tuple[FrozenSubscription, ...]


@dataclass(frozen=True, slots=True)
class PlannedBatch:
    scheduled_at: datetime
    occurrences: tuple[PlannedOccurrence, ...]

    def __post_init__(self):
        if not self.occurrences or any(
            x.scheduled_at != self.scheduled_at for x in self.occurrences
        ):
            raise ValueError("batch occurrences must share scheduled_at")


@dataclass(frozen=True, slots=True)
class ClaimResult:
    status: str
    created: bool = True
    job_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScanResult:
    dispatched: int = 0
    missed: int = 0
    duplicates: int = 0
    failed: int = 0


class OccurrenceStore:
    def __init__(self, session):
        self.session = session

    async def add_many(self, occurrences):
        self.session.add_all(occurrences)
        await self.session.flush()

    async def mark_job_missed(self, job_id: UUID, now: datetime):
        rows = await self.session.scalars(
            update(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.job_id == job_id,
                ScheduleOccurrence.status == OccurrenceStatus.DISPATCHED,
            )
            .values(
                status=OccurrenceStatus.MISSED,
                error_code="SCHEDULE_OCCURRENCE_MISSED",
                updated_at=now,
            )
            .returning(ScheduleOccurrence)
        )
        return tuple(rows.all())

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        occurrence_type: str | None,
        status: str | None,
        scheduled_from: datetime | None,
        scheduled_before: datetime | None,
    ):
        filters = []
        if occurrence_type is not None:
            filters.append(ScheduleOccurrence.occurrence_type == occurrence_type)
        if status is not None:
            filters.append(ScheduleOccurrence.status == status)
        if scheduled_from is not None:
            filters.append(ScheduleOccurrence.scheduled_at >= scheduled_from)
        if scheduled_before is not None:
            filters.append(ScheduleOccurrence.scheduled_at < scheduled_before)
        total = await self.session.scalar(
            select(func.count()).select_from(ScheduleOccurrence).where(*filters)
        )
        rows = await self.session.scalars(
            select(ScheduleOccurrence)
            .where(*filters)
            .order_by(
                ScheduleOccurrence.scheduled_at.desc(),
                ScheduleOccurrence.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        latest_updated_at = await self.session.scalar(
            select(func.max(ScheduleOccurrence.updated_at))
        )
        return tuple(rows.all()), int(total or 0), latest_updated_at


class OccurrenceEventAdapter:
    def __init__(self, session, writer=None):
        self.session = session
        self.writer = writer or TransactionalOutboxWriter()

    async def append(self, occurrence, action):
        await self.writer.append(
            session=self.session,
            topic=f"schedule_occurrence.{action}",
            aggregate_type="schedule_occurrence",
            aggregate_id=str(occurrence.id),
            queue="domain-events",
            payload={
                "event_type": f"schedule_occurrence.{action}",
                "occurrence_id": str(occurrence.id),
                "schedule_id": str(occurrence.schedule_id),
                "schedule_revision_id": str(occurrence.schedule_revision_id),
                "scheduled_at": occurrence.scheduled_at.isoformat(),
                "status": str(occurrence.status),
                "job_id": str(occurrence.job_id) if occurrence.job_id else None,
            },
            dedupe_key=f"schedule-occurrence:{occurrence.schedule_id}:{occurrence.scheduled_at.isoformat()}:{action}",
        )


class MonitorOccurrenceApplication:
    def __init__(
        self,
        database,
        *,
        store_factory=OccurrenceStore,
        event_factory=OccurrenceEventAdapter,
    ):
        self.database = database
        self.store_factory = store_factory
        self.event_factory = event_factory

    async def mark_job_missed(self, job_id: UUID, now: datetime) -> int:
        async with self.database.transaction() as session:
            occurrences = await self.store_factory(session).mark_job_missed(job_id, now)
            events = self.event_factory(session)
            for occurrence in occurrences:
                await events.append(occurrence, "missed")
            return len(occurrences)

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        occurrence_type: str | None = None,
        status: str | None = None,
        from_date: date | None = None,
        through_date: date | None = None,
    ) -> ScheduleOccurrencePageView:
        if page < 1 or not 1 <= page_size <= 200:
            raise ValueError("invalid occurrence pagination")
        if (
            from_date is not None
            and through_date is not None
            and from_date > through_date
        ):
            raise ValueError("invalid occurrence date range")
        scheduled_from = _beijing_day_start(from_date) if from_date else None
        scheduled_before = (
            _beijing_day_start(through_date + timedelta(days=1))
            if through_date
            else None
        )
        async with self.database.session() as session:
            rows, total, latest = await self.store_factory(session).list(
                page=page,
                page_size=page_size,
                occurrence_type=occurrence_type,
                status=status,
                scheduled_from=scheduled_from,
                scheduled_before=scheduled_before,
            )
        return ScheduleOccurrencePageView(
            items=tuple(_occurrence_view(row) for row in rows),
            page=page,
            page_size=page_size,
            total=total,
            latest_updated_at=latest,
        )


def get_monitor_occurrence_application() -> MonitorOccurrenceApplication:
    return MonitorOccurrenceApplication(get_database())


def _beijing_day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC) - timedelta(hours=8)


def _occurrence_view(row) -> ScheduleOccurrenceView:
    return ScheduleOccurrenceView(
        id=row.id,
        occurrence_type=row.occurrence_type,
        schedule_id=row.schedule_id,
        scheduled_at=row.scheduled_at,
        status=OccurrenceStatus(row.status),
        subscriptions=(),
        job_id=row.job_id,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class MonitorScanner:
    def __init__(
        self,
        database,
        calendar,
        schedules,
        subscriptions,
        *,
        job_factory,
        event_factory,
        universe_freezer,
        store_factory=OccurrenceStore,
    ):
        self.database = database
        self.calendar = calendar
        self.schedules = schedules
        self.subscriptions = subscriptions
        self.job_factory = job_factory
        self.event_factory = event_factory
        self.universe_freezer = universe_freezer
        self.store_factory = store_factory

    async def claim(self, plan: PlannedOccurrence, *, now: datetime) -> ClaimResult:
        return await self.claim_batch(PlannedBatch(plan.scheduled_at, (plan,)), now=now)

    async def claim_batch(self, batch: PlannedBatch, *, now: datetime) -> ClaimResult:
        late = now > batch.scheduled_at + timedelta(seconds=60)
        async with self.database.transaction() as session:
            rows = [
                ScheduleOccurrence(
                    id=uuid4(),
                    occurrence_type="REALTIME_QUOTE",
                    schedule_id=plan.schedule_id,
                    schedule_revision_id=plan.schedule_revision_id,
                    scheduled_at=batch.scheduled_at,
                    subscription_snapshot=[
                        x.model_dump(mode="json") for x in plan.subscriptions
                    ],
                    status=OccurrenceStatus.MISSED
                    if late
                    else OccurrenceStatus.PENDING,
                    error_code="SCHEDULE_OCCURRENCE_MISSED" if late else None,
                )
                for plan in batch.occurrences
            ]
            try:
                async with session.begin_nested():
                    await self.store_factory(session).add_many(rows)
            except IntegrityError:
                return ClaimResult("DUPLICATE", False)
            if late:
                for row in rows:
                    await self.event_factory(session).append(row, "missed")
                return ClaimResult("MISSED")
            by_id = {
                x.subscription_id: x
                for plan in batch.occurrences
                for x in plan.subscriptions
            }
            frozen = tuple(
                sorted(by_id.values(), key=lambda x: (x.symbol, str(x.subscription_id)))
            )
            symbols = tuple(x.symbol for x in frozen)
            universe = await self.universe_freezer(session, symbols)
            deadline = batch.scheduled_at + timedelta(seconds=60)
            command = SubmitJob(
                job_type="REALTIME_QUOTE_CYCLE",
                queue="realtime-quotes",
                idempotency_scope="monitor-occurrence-batch",
                idempotency_key=batch.scheduled_at.isoformat(),
                request_id=f"monitor-{rows[0].id}",
                config_snapshot={
                    "occurrence_ids": [str(x.id) for x in rows],
                    "scheduled_at": batch.scheduled_at.isoformat(),
                    "claim_deadline_at": deadline.isoformat(),
                    "subscriptions": [x.model_dump(mode="json") for x in frozen],
                    "symbols": list(symbols),
                    "universe_snapshot_id": str(universe.id),
                    "universe_snapshot_version": universe.master_version,
                    "requested_at": batch.scheduled_at.isoformat(),
                    "timeout_seconds": 30,
                },
                business_object_type="schedule_occurrence_batch",
                business_object_id=batch.scheduled_at.isoformat(),
                soft_timeout_seconds=30,
                hard_timeout_seconds=60,
            )
            job = await self.job_factory(session).submit(command)
            for row in rows:
                row.job_id = job.id
                row.status = OccurrenceStatus.DISPATCHED
                row.claimed_at = now
                row.dispatched_at = now
                await self.event_factory(session).append(row, "created")
            return ClaimResult("DISPATCHED", job_id=job.id)

    async def scan(self, *, now: datetime) -> ScanResult:
        local_date = (now + timedelta(hours=8)).date()
        window = await self.calendar.trading_dates(local_date, local_date)
        if local_date not in window.dates:
            return ScanResult()
        groups = {
            x.schedule_id: x.subscriptions
            for x in await self.subscriptions.enabled_schedule_snapshots()
        }
        batches = {}
        failed = 0
        for schedule in await self.schedules.list():
            subscriptions = groups.get(schedule.id, ())
            if not subscriptions:
                continue
            try:
                revision = await self.schedules.current_revision(schedule.id)
                for at in revision.times:
                    scheduled = datetime.combine(
                        local_date, at, tzinfo=UTC
                    ) - timedelta(hours=8)
                    if scheduled <= now:
                        batches.setdefault(scheduled, []).append(
                            PlannedOccurrence(
                                schedule.id, revision.id, scheduled, subscriptions
                            )
                        )
            except Exception:
                failed += 1
        counts = {"DISPATCHED": 0, "MISSED": 0, "DUPLICATE": 0}
        for scheduled, plans in sorted(batches.items()):
            try:
                counts[
                    (
                        await self.claim_batch(
                            PlannedBatch(scheduled, tuple(plans)), now=now
                        )
                    ).status
                ] += 1
            except Exception:
                failed += 1
        return ScanResult(
            counts["DISPATCHED"], counts["MISSED"], counts["DUPLICATE"], failed
        )
