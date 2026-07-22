from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from long_invest.modules.system_status.runtime import SchedulerRuntimeApplication


class Database:
    @asynccontextmanager
    async def transaction(self):
        yield object()

    @asynccontextmanager
    async def session(self):
        yield object()


class Repository:
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    begun = None
    finished = None
    snapshot = None

    def __init__(self, session) -> None:
        pass

    async def database_time(self):
        return self.now

    async def begin_scan(self, **values):
        type(self).begun = values

    async def finish_scan(self, **values):
        type(self).finished = values

    async def get(self, role):
        return self.snapshot


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("skew", "paused"),
    ((5, False), (5.1, False), (30, False), (30.1, True)),
)
async def test_clock_skew_only_pauses_above_thirty_seconds(
    skew: float, paused: bool
) -> None:
    application = SchedulerRuntimeApplication(
        Database(), repository_factory=Repository
    )

    result = await application.begin_scan(
        instance_id="scheduler-1",
        application_time=Repository.now + timedelta(seconds=skew),
    )

    assert result.automatic_scheduling_paused is paused
    assert result.clock_skew_seconds == pytest.approx(skew)
    assert Repository.begun["paused"] is paused


@pytest.mark.anyio
async def test_finish_scan_is_fenced_by_instance_and_records_failure() -> None:
    application = SchedulerRuntimeApplication(
        Database(), repository_factory=Repository
    )

    await application.finish_scan(
        instance_id="scheduler-2",
        success=False,
        error_code="SCHEDULER_SCAN_FAILED",
    )

    assert Repository.finished["instance_id"] == "scheduler-2"
    assert Repository.finished["success"] is False
    assert Repository.finished["error_code"] == "SCHEDULER_SCAN_FAILED"
