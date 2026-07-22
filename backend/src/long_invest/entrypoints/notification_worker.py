import asyncio
import signal
from contextlib import suppress

import structlog

from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.runtime import NotificationDeliveryRuntime
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.logging.configure import configure_logging

logger = structlog.get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    channel = DeliveryChannel(settings.notification_channel)
    runtime = NotificationDeliveryRuntime(database, settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)
    try:
        while not stop.is_set():
            try:
                worked = await runtime.process_once(channel)
            except Exception:
                logger.exception(
                    "notification_delivery_cycle_failed",
                    category="notifications",
                    channel=channel.value,
                )
                worked = False
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=settings.notification_worker_poll_seconds,
                    )
    finally:
        await database.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service=f"longinvest-notification-{settings.notification_channel.lower()}",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
