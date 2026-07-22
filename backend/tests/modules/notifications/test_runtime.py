import asyncio
from types import SimpleNamespace

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    DeliveryOutcome,
)
from long_invest.modules.notifications.runtime import NotificationDeliveryRuntime
from long_invest.platform.config.settings import AppSettings


def runtime() -> NotificationDeliveryRuntime:
    return NotificationDeliveryRuntime(
        SimpleNamespace(),
        AppSettings(environment="test", master_key=""),
    )


def test_pre_send_review_rejects_changed_channel_configuration() -> None:
    subject = runtime()
    config = {
        "value": {"enabled": True, "timeout_seconds": 5},
        "version": 2,
    }
    secret = {
        "configured": True,
        "fingerprint": "fingerprint",
        "version": 1,
    }
    event = SimpleNamespace(
        status="DISPATCHED",
        event_type="signal.low",
        template_variables={},
    )
    delivery = SimpleNamespace(
        channel="WECOM",
        config_version=1,
        target_fingerprint="old",
    )

    result = asyncio.run(subject._reviewer(config, secret)(event, delivery))

    assert result.eligible is False
    assert result.reason == "CHANNEL_CONFIGURATION_CHANGED"
    assert result.delivery_status == "SKIPPED_DISABLED"


def test_channel_test_does_not_send_when_channel_is_disabled() -> None:
    subject = runtime()

    async def load(_channel):
        return (
            {"value": {"enabled": False}, "version": 1},
            {"configured": False},
            None,
        )

    subject._load_channel = load
    result = asyncio.run(subject.test_channel(DeliveryChannel.WECOM, message="test"))

    assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE
    assert result.code == "NOTIFICATION_CHANNEL_DISABLED"
