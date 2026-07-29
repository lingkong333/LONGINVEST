import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete

from long_invest.modules.system_status.models import SchedulerRuntimeState
from long_invest.modules.system_status.runtime import SchedulerRuntimeApplication
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.database.notifications import (
    PostgresNotificationListener,
    notify_scheduler_refresh,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_committed_database_notification_refreshes_scheduler() -> None:
    settings = AppSettings(_env_file=None)
    database = Database(settings.database_url)
    listener = PostgresNotificationListener(settings.database_url)
    stop = asyncio.Event()
    refreshed = asyncio.Event()

    async def refresh() -> None:
        refreshed.set()

    task = asyncio.create_task(listener.run(stop, refresh))
    try:
        async with asyncio.timeout(3):
            while not refreshed.is_set():
                async with database.transaction() as session:
                    await notify_scheduler_refresh(
                        session, reason=f"integration:{uuid4().hex}"
                    )
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await task
        await database.dispose()


@pytest.mark.anyio
async def test_scheduler_runtime_plan_is_visible_to_application_role() -> None:
    settings = AppSettings(_env_file=None)
    database = Database(settings.database_url)
    role = f"scheduler-test-{uuid4().hex}"
    runtime = SchedulerRuntimeApplication(database, role=role)
    try:
        decision = await runtime.begin_scan(
            instance_id="integration-scheduler",
            application_time=datetime.now(UTC),
        )
        await runtime.finish_scan(
            instance_id="integration-scheduler",
            success=True,
            intraday_plan=[
                {
                    "key": "2026-07-29T14:30",
                    "next_run_at": "2026-07-29T06:30:00+00:00",
                    "timezone": "Asia/Shanghai",
                }
            ],
            persistent_plan=[
                {
                    "key": "daily-market-data",
                    "next_run_at": "2026-07-29T09:00:00+00:00",
                    "timezone": "Asia/Shanghai",
                }
            ],
        )
        snapshot = await runtime.get()
        assert decision.automatic_scheduling_paused is False
        assert snapshot is not None
        assert snapshot.intraday_plan[0]["key"] == "2026-07-29T14:30"
        assert snapshot.persistent_plan[0]["key"] == "daily-market-data"
    finally:
        async with database.transaction() as session:
            await session.execute(
                delete(SchedulerRuntimeState).where(
                    SchedulerRuntimeState.role == role
                )
            )
        await database.dispose()
