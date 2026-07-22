import asyncio
from datetime import date
from types import SimpleNamespace

from long_invest.modules.alerts.integrations import (
    SystemAlertNotificationPublisher,
    _is_failed_channel,
)
from long_invest.modules.notifications.contracts import DeliveryChannel


def test_notification_channel_failure_excludes_only_failed_channel() -> None:
    alert = SimpleNamespace(
        alert_type="NOTIFICATION_CHANNEL_FAILED",
        object_type="notification_channel",
        object_id="WECOM",
    )

    assert _is_failed_channel(alert, DeliveryChannel.WECOM) is True
    assert _is_failed_channel(alert, DeliveryChannel.EMAIL) is False


def test_unrelated_alert_does_not_exclude_notification_channels() -> None:
    alert = SimpleNamespace(
        alert_type="QUOTE_MISSING",
        object_type="quote_cycle",
        object_id="cycle-1",
    )

    assert _is_failed_channel(alert, DeliveryChannel.WECOM) is False
    assert _is_failed_channel(alert, DeliveryChannel.EMAIL) is False


def test_daily_reminder_uses_alert_and_date_as_idempotency_key() -> None:
    class Settings:
        async def get_setting(self, _key):
            return {"value": {"enabled": True, "daily_unresolved": []}}

        async def secret_statuses(self):
            return []

    class Notifications:
        def __init__(self):
            self.commands = []

        async def publish(self, command):
            self.commands.append(command)

    async def scenario():
        publisher = object.__new__(SystemAlertNotificationPublisher)
        publisher._settings = Settings()
        publisher._notifications = Notifications()
        alert = SimpleNamespace(
            id="alert-1",
            alert_type="QUOTE_MISSING",
            object_type="quote_cycle",
            object_id="cycle-1",
            severity="ERROR",
            summary="行情缺失",
        )
        reminder_date = date(2026, 7, 22)

        await publisher.publish_daily_unresolved(
            alert, reminder_date=reminder_date, request_id="request-1"
        )
        await publisher.publish_daily_unresolved(
            alert, reminder_date=reminder_date, request_id="request-2"
        )

        commands = publisher._notifications.commands
        assert [item.idempotency_key for item in commands] == [
            "alert-daily:alert-1:2026-07-22",
            "alert-daily:alert-1:2026-07-22",
        ]
        assert [item.business_event_id for item in commands] == [
            "alert-1:2026-07-22",
            "alert-1:2026-07-22",
        ]

    asyncio.run(scenario())
