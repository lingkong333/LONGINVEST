import asyncio
import signal
from contextlib import suppress

import structlog

from long_invest.modules.signals.projector import SignalEventProjector
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.logging.configure import configure_logging

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    projector = SignalEventProjector(database)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)
    try:
        while not stop.is_set():
            try:
                report = await projector.project_once(
                    limit=settings.dispatcher_batch_size
                )
                if report.claimed:
                    logger.info(
                        "signal_projection_cycle",
                        category="signals",
                        claimed=report.claimed,
                        projected=report.projected,
                    )
            except Exception:
                logger.exception("signal_projection_failed", category="signals")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=settings.dispatcher_scan_interval_seconds,
                )
    finally:
        await database.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service="longinvest-signal-projector",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
