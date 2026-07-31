from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.scheduling.runtime import (
    DAILY_MARKET_DATA,
    DailyGapPlan,
    DualPathScheduler,
    PostgresPersistentJobSubmitter,
)


class Calendar:
    def __init__(self, trading: bool = True) -> None:
        self.trading = trading
        self.version_id = uuid4()

    async def trading_dates(self, start, end):
        return SimpleNamespace(
            dates=(start,) if self.trading else (),
            version_id=self.version_id,
        )


class Schedules:
    def __init__(self, times: tuple[time, ...]) -> None:
        self.times = times
        self.schedule_id = uuid4()

    async def list(self):
        return (SimpleNamespace(id=self.schedule_id),)

    async def current_revision(self, schedule_id):
        assert schedule_id == self.schedule_id
        return SimpleNamespace(times=self.times)


class Runtime:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.finished = []

    async def begin_scan(self, **_kwargs):
        return SimpleNamespace(
            database_time=self.now,
            automatic_scheduling_paused=False,
        )

    async def finish_scan(self, **values):
        self.finished.append(values)


class Submitter:
    def __init__(self) -> None:
        self.calls = []
        self.recovery_calls = []

    async def submit(self, plan, **values):
        self.calls.append((plan, values))

    async def submit_recovery(self, dates, **values):
        self.recovery_calls.append((dates, values))


class GapPlanner:
    def __init__(self, dates: tuple[date, ...]) -> None:
        self.dates = dates
        self.version_id = uuid4()
        self.before = None

    async def plan(self, *, before):
        self.before = before
        if not self.dates:
            return None
        return DailyGapPlan(self.dates, self.version_id)


@pytest.mark.anyio
async def test_daily_submission_survives_several_process_restarts(monkeypatch):
    captured = []

    class Database:
        @asynccontextmanager
        async def transaction(self):
            yield object()

    class Jobs:
        def __init__(self, session):
            del session

        async def submit(self, command, *, now):
            captured.append((command, now))

    monkeypatch.setattr(
        "long_invest.modules.scheduling.runtime.PostgresJobService", Jobs
    )
    scheduled_at = datetime(2026, 7, 31, 9, tzinfo=UTC)

    await PostgresPersistentJobSubmitter(Database()).submit(
        DAILY_MARKET_DATA,
        trade_date=date(2026, 7, 31),
        calendar_version_id=uuid4(),
        scheduled_at=scheduled_at,
    )

    command, submitted_at = captured[0]
    assert submitted_at == scheduled_at
    assert command.max_attempts == 5
    assert command.max_recoveries == 4


def subject(
    now: datetime,
    *,
    times: tuple[time, ...] = (time(10, 15),),
    trading: bool = True,
):
    runtime = Runtime(now)
    schedules = Schedules(times)
    submitter = Submitter()
    triggered = []

    async def intraday(scheduled_at):
        triggered.append(scheduled_at)

    scheduler = DualPathScheduler(
        calendar=Calendar(trading),
        schedules=schedules,
        runtime=runtime,
        persistent_submitter=submitter,
        intraday_handler=intraday,
        instance_id="test-scheduler",
    )
    return scheduler, runtime, schedules, submitter, triggered


@pytest.mark.anyio
async def test_refresh_registers_only_future_intraday_times_and_replaces_changes():
    now = datetime(2026, 7, 17, 2, tzinfo=UTC)
    scheduler, _, schedules, _, _ = subject(
        now,
        times=(time(9, 30), time(10, 15)),
    )
    try:
        await scheduler.start(now=now)
        intraday, _ = scheduler.plan_snapshot()
        assert [item["key"] for item in intraday] == ["2026-07-17T10:15"]

        schedules.times = (time(10, 30),)
        await scheduler.refresh(now=now)
        intraday, _ = scheduler.plan_snapshot()
        assert [item["key"] for item in intraday] == ["2026-07-17T10:30"]
    finally:
        await scheduler.stop()


@pytest.mark.anyio
async def test_intraday_trigger_runs_in_memory_without_persistent_submission():
    scheduled_at = datetime(2026, 7, 17, 2, 15, tzinfo=UTC)
    scheduler, runtime, _, submitter, triggered = subject(scheduled_at)

    await scheduler._fire_intraday(scheduled_at=scheduled_at)

    assert triggered == [scheduled_at]
    assert submitter.calls == []
    assert runtime.finished[-1]["success"] is True


@pytest.mark.anyio
async def test_late_intraday_trigger_is_dropped_without_catch_up():
    scheduled_at = datetime(2026, 7, 17, 2, 15, tzinfo=UTC)
    scheduler, runtime, _, submitter, triggered = subject(scheduled_at)
    runtime.now = scheduled_at + timedelta(seconds=61)

    await scheduler._fire_intraday(scheduled_at=scheduled_at)

    assert triggered == []
    assert submitter.calls == []


@pytest.mark.anyio
async def test_startup_recovers_due_persistent_plan_on_trading_day():
    now = datetime(2026, 7, 17, 9, 1, tzinfo=UTC)
    scheduler, _, _, submitter, _ = subject(now)

    await scheduler.recover_persistent(now=now)

    plan, values = submitter.calls[0]
    assert plan is DAILY_MARKET_DATA
    assert values["trade_date"] == date(2026, 7, 17)
    assert values["scheduled_at"] == datetime(2026, 7, 17, 9, tzinfo=UTC)


@pytest.mark.anyio
async def test_startup_submission_conflict_does_not_stop_scheduler():
    now = datetime(2026, 7, 17, 9, 1, tzinfo=UTC)
    scheduler, runtime, _, submitter, _ = subject(now)

    async def reject_existing_definition(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("idempotency definition changed")

    submitter.submit = reject_existing_definition

    await scheduler.recover_persistent(now=now)

    assert runtime.finished[-1]["success"] is False
    assert runtime.finished[-1]["error_code"] == "PERSISTENT_RECOVERY_PARTIAL"


@pytest.mark.anyio
async def test_startup_merges_missing_dates_into_one_recovery_job():
    now = datetime(2026, 7, 17, 9, 1, tzinfo=UTC)
    scheduler, _, _, submitter, _ = subject(now)
    gap_planner = GapPlanner((date(2026, 7, 14), date(2026, 7, 16)))
    scheduler._daily_gap_planner = gap_planner

    await scheduler.recover_persistent(now=now)

    assert gap_planner.before == date(2026, 7, 16)
    assert len(submitter.recovery_calls) == 1
    dates, values = submitter.recovery_calls[0]
    assert dates == (date(2026, 7, 14), date(2026, 7, 16))
    assert values["calendar_version_id"] == gap_planner.version_id
    assert len(submitter.calls) == 1


@pytest.mark.anyio
async def test_non_trading_day_has_no_intraday_or_persistent_work():
    now = datetime(2026, 7, 18, 9, 1, tzinfo=UTC)
    scheduler, _, _, submitter, triggered = subject(now, trading=False)
    try:
        await scheduler.start(now=now)
        intraday, _ = scheduler.plan_snapshot()
        assert intraday == []
        assert submitter.calls == []
        assert triggered == []
    finally:
        await scheduler.stop()


@pytest.mark.anyio
async def test_broken_schedule_does_not_block_other_intraday_times():
    now = datetime(2026, 7, 17, 2, tzinfo=UTC)
    scheduler, runtime, schedules, _, _ = subject(now)
    healthy_id = schedules.schedule_id
    broken_id = uuid4()

    async def list_schedules():
        return (
            SimpleNamespace(id=broken_id),
            SimpleNamespace(id=healthy_id),
        )

    async def revision(schedule_id):
        if schedule_id == broken_id:
            raise RuntimeError("invalid schedule")
        return SimpleNamespace(times=(time(10, 15),))

    schedules.list = list_schedules
    schedules.current_revision = revision
    try:
        await scheduler.start(now=now)
        intraday, _ = scheduler.plan_snapshot()
        assert [item["key"] for item in intraday] == ["2026-07-17T10:15"]
        assert runtime.finished[-2]["success"] is False
        assert runtime.finished[-2]["error_code"] == ("INTRADAY_PLAN_REFRESH_PARTIAL")
    finally:
        await scheduler.stop()
