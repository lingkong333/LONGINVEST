import asyncio
import os
import signal
import socket
from contextlib import suppress
from datetime import datetime

import structlog

from long_invest.bootstrap.providers import (
    build_provider_service,
    close_provider_resources,
)
from long_invest.modules.calendar.application import CalendarApplication
from long_invest.modules.daily_data.jobs import (
    DailyMarketRecoveryJob,
    FullMarketDailyJob,
)
from long_invest.modules.monitor_schedules.application import MonitorScheduleApplication
from long_invest.modules.scheduling.runtime import (
    DailyGapPlanner,
    DualPathScheduler,
    PostgresPersistentJobSubmitter,
)
from long_invest.modules.system_status.runtime import SchedulerRuntimeApplication
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.database.notifications import PostgresNotificationListener
from long_invest.platform.jobs.postgres_runner import PostgresJobRunner
from long_invest.platform.logging.configure import configure_logging

HEARTBEAT_SECONDS = 5
logger = structlog.get_logger(__name__)


async def _intraday_foundation(scheduled_at: datetime) -> None:
    logger.info(
        "intraday_schedule_triggered",
        category="scheduler",
        scheduled_at=scheduled_at.isoformat(),
    )


async def _heartbeat(scheduler: DualPathScheduler, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await scheduler.heartbeat()
        except Exception:
            logger.exception("scheduler_heartbeat_failed", category="scheduler")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)


async def run() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    runtime = SchedulerRuntimeApplication(database)
    scheduler = DualPathScheduler(
        calendar=CalendarApplication(database),
        schedules=MonitorScheduleApplication(database),
        runtime=runtime,
        persistent_submitter=PostgresPersistentJobSubmitter(database),
        intraday_handler=_intraday_foundation,
        instance_id=f"{socket.gethostname()}:{os.getpid()}",
        daily_gap_planner=DailyGapPlanner(
            database, CalendarApplication(database)
        ),
    )
    worker_id = f"daily-market:{socket.gethostname()}:{os.getpid()}"
    runner = PostgresJobRunner(
        database,
        {
            "DAILY_MARKET_DATA": FullMarketDailyJob(
                database,
                provider_service_factory=build_provider_service,
            ),
            "DAILY_MARKET_RECOVERY": DailyMarketRecoveryJob(
                database,
                provider_service_factory=build_provider_service,
            ),
        },
        worker_id=worker_id,
    )
    listener = PostgresNotificationListener(settings.database_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)

    tasks: list[asyncio.Task[None]] = []
    try:
        await scheduler.start()
        tasks = [
            asyncio.create_task(listener.run(stop, scheduler.refresh)),
            asyncio.create_task(_heartbeat(scheduler, stop)),
            asyncio.create_task(runner.run_forever(stop)),
        ]
        await stop.wait()
    finally:
        stop.set()
        await scheduler.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await close_provider_resources()
        await database.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service="longinvest-monitor-scheduler",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
