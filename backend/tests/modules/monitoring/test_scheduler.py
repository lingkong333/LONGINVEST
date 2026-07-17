from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.monitoring.contracts import FrozenSubscription
from long_invest.modules.monitoring.scheduler import (
    MonitorScanner,
    PlannedBatch,
    PlannedOccurrence,
)


@pytest.mark.anyio
async def test_late_occurrence_is_missed_and_never_dispatched() -> None:
    scheduled = datetime(2026, 7, 17, 2, 15, tzinfo=UTC)

    class Session:
        @asynccontextmanager
        async def begin_nested(self):
            yield

    class Database:
        @asynccontextmanager
        async def transaction(self):
            yield Session()

    class Store:
        def __init__(self, session):
            pass

        async def add_many(self, item):
            pass

    class Events:
        def __init__(self, session):
            pass

        async def append(self, item, action):
            pass

    class Jobs:
        submissions = []

        def __init__(self, session):
            pass

        async def submit(self, item):
            self.submissions.append(item)

    scanner = MonitorScanner(
        Database(),
        None,
        None,
        None,
        job_factory=Jobs,
        event_factory=Events,
        universe_freezer=lambda symbols: None,
        store_factory=Store,
    )
    result = await scanner.claim(
        PlannedOccurrence(uuid4(), uuid4(), scheduled, ()),
        now=scheduled + timedelta(seconds=61),
    )
    assert result.status == "MISSED"
    assert Jobs.submissions == []


@pytest.mark.anyio
async def test_frozen_subscription_versions_flow_into_job_config() -> None:
    scheduled = datetime(2026, 7, 17, 2, 15, tzinfo=UTC)
    subscription = FrozenSubscription(
        subscription_id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        version=7,
        revision_id=uuid4(),
    )

    class Session:
        @asynccontextmanager
        async def begin_nested(self):
            yield

    class Database:
        @asynccontextmanager
        async def transaction(self):
            yield Session()

    class Store:
        def __init__(self, session):
            pass

        async def add_many(self, item):
            pass

    class Events:
        def __init__(self, session):
            pass

        async def append(self, item, action):
            pass

    class Jobs:
        command = None

        def __init__(self, session):
            pass

        async def submit(self, item):
            type(self).command = item
            return SimpleNamespace(id=uuid4())

    async def freezer(session, symbols):
        return SimpleNamespace(id=uuid4(), master_version=11)

    scanner = MonitorScanner(
        Database(),
        None,
        None,
        None,
        job_factory=Jobs,
        event_factory=Events,
        universe_freezer=freezer,
        store_factory=Store,
    )
    await scanner.claim(
        PlannedOccurrence(uuid4(), uuid4(), scheduled, (subscription,)), now=scheduled
    )
    frozen = Jobs.command.config_snapshot["subscriptions"][0]
    assert frozen["version"] == 7 and frozen["revision_id"] == str(
        subscription.revision_id
    )
    assert Jobs.command.config_snapshot["symbols"] == ["600000.SH"]
    assert (
        Jobs.command.config_snapshot["claim_deadline_at"]
        == (scheduled + timedelta(seconds=60)).isoformat()
    )


@pytest.mark.anyio
async def test_different_scheduled_times_create_independent_jobs() -> None:
    scheduled = datetime(2026, 7, 17, 2, 15, tzinfo=UTC)
    subscription = FrozenSubscription(
        subscription_id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        version=1,
        revision_id=uuid4(),
    )

    class Session:
        @asynccontextmanager
        async def begin_nested(self):
            yield

    class Database:
        @asynccontextmanager
        async def transaction(self):
            yield Session()

    class Store:
        def __init__(self, session):
            pass

        async def add_many(self, items):
            pass

    class Events:
        def __init__(self, session):
            pass

        async def append(self, item, action):
            pass

    class Jobs:
        commands = []

        def __init__(self, session):
            pass

        async def submit(self, command):
            self.commands.append(command)
            return SimpleNamespace(id=uuid4())

    async def freezer(session, symbols):
        return SimpleNamespace(id=uuid4(), master_version=1)

    scanner = MonitorScanner(
        Database(),
        None,
        None,
        None,
        job_factory=Jobs,
        event_factory=Events,
        universe_freezer=freezer,
        store_factory=Store,
    )
    schedule_id = uuid4()
    revision_id = uuid4()
    results = []
    for offset in (0, 1):
        at = scheduled + timedelta(minutes=offset)
        results.append(
            await scanner.claim(
                PlannedOccurrence(schedule_id, revision_id, at, (subscription,)),
                now=at,
            )
        )

    assert all(item.status == "DISPATCHED" for item in results)
    assert results[0].job_id != results[1].job_id
    assert [item.idempotency_key for item in Jobs.commands] == [
        scheduled.isoformat(),
        (scheduled + timedelta(minutes=1)).isoformat(),
    ]


@pytest.mark.anyio
async def test_same_time_across_schedules_creates_one_merged_job() -> None:
    scheduled = datetime(2026, 7, 17, 2, 15, tzinfo=UTC)
    first = FrozenSubscription(
        subscription_id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        version=2,
        revision_id=uuid4(),
    )
    second = FrozenSubscription(
        subscription_id=uuid4(),
        security_id=uuid4(),
        symbol="000001.SZ",
        version=3,
        revision_id=uuid4(),
    )
    batch = PlannedBatch(
        scheduled,
        (
            PlannedOccurrence(uuid4(), uuid4(), scheduled, (first,)),
            PlannedOccurrence(uuid4(), uuid4(), scheduled, (second,)),
        ),
    )
    assert len(batch.occurrences) == 2
    assert {x.symbol for item in batch.occurrences for x in item.subscriptions} == {
        "600000.SH",
        "000001.SZ",
    }


@pytest.mark.anyio
async def test_scan_uses_public_windows_and_merges_frozen_subscriptions() -> None:
    day = date(2026, 7, 17)
    schedule_id = uuid4()
    revision_id = uuid4()

    class Calendar:
        async def trading_dates(self, start, end):
            return SimpleNamespace(dates=(day,))

    class Schedules:
        async def list(self):
            return (SimpleNamespace(id=schedule_id),)

        async def current_revision(self, id):
            return SimpleNamespace(id=revision_id, times=(time(10, 15), time(10, 16)))

    frozen = (object(), object())
    subscriptions = (SimpleNamespace(schedule_id=schedule_id, subscriptions=frozen),)

    class Subs:
        async def enabled_schedule_snapshots(self):
            return subscriptions

    scanner = MonitorScanner(
        None,
        Calendar(),
        Schedules(),
        Subs(),
        job_factory=None,
        event_factory=None,
        universe_freezer=None,
    )
    calls = []

    async def claim(batch, *, now):
        calls.extend(batch.occurrences)
        return SimpleNamespace(status="DISPATCHED")

    scanner.claim_batch = claim
    result = await scanner.scan(now=datetime(2026, 7, 17, 2, 16, tzinfo=UTC))
    assert result.dispatched == 2
    assert [item.schedule_revision_id for item in calls] == [revision_id, revision_id]
    assert all(item.subscriptions == frozen for item in calls)


@pytest.mark.anyio
async def test_non_trading_day_creates_no_plan() -> None:
    class Calendar:
        async def trading_dates(self, start, end):
            return SimpleNamespace(dates=())

    scanner = MonitorScanner(
        None,
        Calendar(),
        None,
        None,
        job_factory=None,
        event_factory=None,
        universe_freezer=None,
    )
    result = await scanner.scan(now=datetime(2026, 7, 17, 2, 15, tzinfo=UTC))
    assert result.dispatched == result.missed == result.duplicates == 0


@pytest.mark.anyio
async def test_future_and_schedule_without_enabled_subscriptions_are_skipped() -> None:
    day = date(2026, 7, 17)
    active = uuid4()
    empty = uuid4()

    class Calendar:
        async def trading_dates(self, start, end):
            return SimpleNamespace(dates=(day,))

    class Schedules:
        async def list(self):
            return (SimpleNamespace(id=active), SimpleNamespace(id=empty))

        async def current_revision(self, id):
            return SimpleNamespace(id=uuid4(), times=(time(10, 16),))

    class Subs:
        async def enabled_schedule_snapshots(self):
            return (SimpleNamespace(schedule_id=active, subscriptions=(object(),)),)

    scanner = MonitorScanner(
        None,
        Calendar(),
        Schedules(),
        Subs(),
        job_factory=None,
        event_factory=None,
        universe_freezer=None,
    )
    calls = []

    async def claim(batch, *, now):
        calls.extend(batch.occurrences)
        return SimpleNamespace(status="DISPATCHED")

    scanner.claim_batch = claim
    result = await scanner.scan(now=datetime(2026, 7, 17, 2, 15, tzinfo=UTC))
    assert result.dispatched == 0 and calls == []


@pytest.mark.anyio
async def test_one_schedule_failure_does_not_block_another() -> None:
    day = date(2026, 7, 17)
    broken = uuid4()
    healthy = uuid4()

    class Calendar:
        async def trading_dates(self, start, end):
            return SimpleNamespace(dates=(day,))

    class Schedules:
        async def list(self):
            return (SimpleNamespace(id=broken), SimpleNamespace(id=healthy))

        async def current_revision(self, id):
            if id == broken:
                raise RuntimeError("broken revision")
            return SimpleNamespace(id=uuid4(), times=(time(10, 15),))

    class Subs:
        async def enabled_schedule_snapshots(self):
            return tuple(
                SimpleNamespace(schedule_id=id, subscriptions=(object(),))
                for id in (broken, healthy)
            )

    scanner = MonitorScanner(
        None,
        Calendar(),
        Schedules(),
        Subs(),
        job_factory=None,
        event_factory=None,
        universe_freezer=None,
    )
    calls = []

    async def claim(batch, *, now):
        calls.extend(batch.occurrences)
        return SimpleNamespace(status="DISPATCHED")

    scanner.claim_batch = claim
    result = await scanner.scan(now=datetime(2026, 7, 17, 2, 15, tzinfo=UTC))
    assert result.failed == 1 and result.dispatched == 1
    assert calls[0].schedule_id == healthy
