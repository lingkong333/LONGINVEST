import asyncio

import pytest

from long_invest.platform.database.notifications import (
    SCHEDULER_REFRESH_CHANNEL,
    PostgresNotificationListener,
)


class Connection:
    def __init__(self) -> None:
        self.listener = None
        self.closed = False

    async def add_listener(self, channel, callback):
        assert channel == SCHEDULER_REFRESH_CHANNEL
        self.listener = callback

    async def remove_listener(self, channel, callback):
        assert channel == SCHEDULER_REFRESH_CHANNEL
        assert callback is self.listener

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_postgres_notification_refreshes_without_polling():
    connection = Connection()
    connected = asyncio.Event()

    async def connect(**_kwargs):
        connected.set()
        return connection

    listener = PostgresNotificationListener(
        "postgresql+asyncpg://user:password@postgres/database",
        connect=connect,
    )
    stop = asyncio.Event()
    refreshed = asyncio.Event()

    async def refresh():
        refreshed.set()

    task = asyncio.create_task(listener.run(stop, refresh))
    await connected.wait()
    while connection.listener is None:
        await asyncio.sleep(0)

    connection.listener(connection, 1, SCHEDULER_REFRESH_CHANNEL, "changed")
    await asyncio.wait_for(refreshed.wait(), timeout=1)
    stop.set()
    await task

    assert connection.closed is True
