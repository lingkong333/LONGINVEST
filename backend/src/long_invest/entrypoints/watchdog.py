import asyncio
import signal
from contextlib import suppress
from datetime import timedelta

import structlog

from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.watchdog import JobsWatchdog
from long_invest.platform.logging.configure import configure_logging

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)
    watchdog = JobsWatchdog(
        database=database,
        outbox_lease_timeout=timedelta(
            seconds=settings.outbox_lease_timeout_seconds
        ),
        run_stale_timeout=timedelta(seconds=settings.run_stale_timeout_seconds),
    )
    try:
        while not stop.is_set():
            report = await watchdog.recover_once()
            if report.outbox_leases_released or report.runs_lost:
                logger.warning(
                    "jobs_watchdog_recovery",
                    category="maintenance",
                    outbox_leases_released=report.outbox_leases_released,
                    runs_lost=report.runs_lost,
                    recoveries_scheduled=report.recoveries_scheduled,
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=settings.watchdog_scan_interval_seconds,
                )
    finally:
        await database.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service="longinvest-watchdog",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
