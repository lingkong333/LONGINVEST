import asyncio
import signal
from contextlib import suppress
from datetime import UTC, datetime

import structlog

from long_invest.modules.calendar.application import CalendarApplication
from long_invest.modules.monitor_schedules.application import MonitorScheduleApplication
from long_invest.modules.monitoring.application import MonitorSubscriptionApplication
from long_invest.modules.monitoring.scheduler import (
    MonitorScanner,
    OccurrenceEventAdapter,
)
from long_invest.modules.securities.application import SecurityApplication
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.service import JobService
from long_invest.platform.logging.configure import configure_logging

SCAN_INTERVAL_SECONDS = 10
logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    security = SecurityApplication(database)
    schedules = MonitorScheduleApplication(database)
    subscriptions = MonitorSubscriptionApplication(
        database,
        security_application=security,
        schedule_application=schedules,
    )
    scanner = MonitorScanner(
        database,
        CalendarApplication(database),
        schedules,
        subscriptions,
        job_factory=JobService,
        event_factory=OccurrenceEventAdapter,
        universe_freezer=security.freeze_symbols_in_transaction,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)
    try:
        while not stop.is_set():
            try:
                report = await scanner.scan(now=datetime.now(UTC))
                if report.dispatched or report.missed or report.failed:
                    logger.info(
                        "monitor_scheduler_scan",
                        category="maintenance",
                        dispatched=report.dispatched,
                        missed=report.missed,
                        duplicates=report.duplicates,
                        failed=report.failed,
                    )
            except Exception:
                logger.exception(
                    "monitor_scheduler_scan_failed", category="maintenance"
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_SECONDS)
    finally:
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
