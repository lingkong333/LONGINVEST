import asyncio

from long_invest.entrypoints.monitor_scheduler import _run_notification_channel
from long_invest.modules.notifications.contracts import DeliveryChannel


class _Runtime:
    def __init__(self, stop: asyncio.Event, *, error: Exception | None = None) -> None:
        self.stop = stop
        self.error = error
        self.channels: list[DeliveryChannel] = []

    async def process_once(self, channel: DeliveryChannel) -> bool:
        self.channels.append(channel)
        self.stop.set()
        if self.error is not None:
            raise self.error
        return True


def test_notification_channel_is_processed_inside_background() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        runtime = _Runtime(stop)

        await _run_notification_channel(
            runtime,
            DeliveryChannel.WECOM,
            stop=stop,
            poll_seconds=0.01,
        )

        assert runtime.channels == [DeliveryChannel.WECOM]

    asyncio.run(scenario())


def test_notification_channel_failure_is_isolated() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        runtime = _Runtime(stop, error=RuntimeError("temporary failure"))

        await _run_notification_channel(
            runtime,
            DeliveryChannel.EMAIL,
            stop=stop,
            poll_seconds=0.01,
        )

        assert runtime.channels == [DeliveryChannel.EMAIL]

    asyncio.run(scenario())
