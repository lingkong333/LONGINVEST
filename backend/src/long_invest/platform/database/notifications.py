from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

import asyncpg
import structlog
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

SCHEDULER_REFRESH_CHANNEL = "longinvest_scheduler_refresh"
logger = structlog.get_logger(__name__)


async def notify_scheduler_refresh(
    session: AsyncSession,
    *,
    reason: str,
) -> None:
    await session.execute(
        select(func.pg_notify(SCHEDULER_REFRESH_CHANNEL, reason[:200]))
    )


class PostgresNotificationListener:
    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[..., Awaitable[asyncpg.Connection]] = asyncpg.connect,
        reconnect_seconds: float = 2,
    ) -> None:
        if reconnect_seconds <= 0:
            raise ValueError("reconnect delay must be positive")
        url = make_url(database_url).set(drivername="postgresql")
        self._dsn = url.render_as_string(hide_password=False)
        self._connect = connect
        self._reconnect_seconds = reconnect_seconds

    async def run(
        self,
        stop: asyncio.Event,
        on_refresh: Callable[[], Awaitable[None]],
    ) -> None:
        while not stop.is_set():
            connection: asyncpg.Connection | None = None
            wake = asyncio.Event()

            def receive(*_args: object, notification: asyncio.Event = wake) -> None:
                notification.set()

            try:
                connection = await self._connect(dsn=self._dsn)
                await connection.add_listener(SCHEDULER_REFRESH_CHANNEL, receive)
                while not stop.is_set():
                    stop_wait = asyncio.create_task(stop.wait())
                    refresh_wait = asyncio.create_task(wake.wait())
                    done, pending = await asyncio.wait(
                        {stop_wait, refresh_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if stop_wait in done:
                        return
                    wake.clear()
                    await on_refresh()
            except Exception:
                logger.exception(
                    "scheduler_notification_listener_failed",
                    category="scheduler",
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=self._reconnect_seconds
                    )
            finally:
                if connection is not None:
                    with suppress(Exception):
                        await connection.remove_listener(
                            SCHEDULER_REFRESH_CHANNEL, receive
                        )
                    with suppress(Exception):
                        await connection.close()
