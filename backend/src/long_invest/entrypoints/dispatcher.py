import asyncio
import signal
import socket
from contextlib import suppress

import structlog

from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.logging.configure import configure_logging
from long_invest.platform.outbox.dispatcher import OutboxDispatcher
from long_invest.platform.queue.rq import RqQueuePublisher

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    publisher = RqQueuePublisher(settings.redis_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)
    dispatcher = OutboxDispatcher(
        database=database,
        publisher=publisher,
        dispatcher_id=socket.gethostname(),
        batch_size=settings.dispatcher_batch_size,
        queue_timeout_seconds=settings.queue_job_timeout_seconds,
    )
    try:
        while not stop.is_set():
            report = await dispatcher.dispatch_once()
            if report.claimed:
                logger.info(
                    "outbox_dispatch_cycle",
                    category="maintenance",
                    claimed=report.claimed,
                    dispatched=report.dispatched,
                    failed=report.failed,
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=settings.dispatcher_scan_interval_seconds,
                )
    finally:
        await publisher.close()
        await database.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service="longinvest-dispatcher",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
