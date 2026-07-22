from __future__ import annotations

from datetime import date

from long_invest.modules.alerts.contracts import AlertSeverity
from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.service import (
    ChannelDeliveryTarget,
    PublishNotification,
    transactional_notification_service,
)
from long_invest.modules.notifications.targets import target_fingerprint
from long_invest.modules.settings.service import transactional_settings_service

_CONFIG_KEYS = {
    DeliveryChannel.WECOM: "notification.channel.wecom",
    DeliveryChannel.EMAIL: "notification.channel.email",
}
_SECRET_KEYS = {
    DeliveryChannel.WECOM: "notification.wecom.webhook",
    DeliveryChannel.EMAIL: "notification.email.password",
}
_TEMPLATES = {
    AlertSeverity.INFO: "system.warning",
    AlertSeverity.WARNING: "system.warning",
    AlertSeverity.ERROR: "system.error",
    AlertSeverity.CRITICAL: "system.critical",
}


class SystemAlertNotificationPublisher:
    def __init__(self, session) -> None:
        self._notifications = transactional_notification_service(session)
        self._settings = transactional_settings_service(session)

    async def publish(self, alert, *, recovered: bool, request_id: str) -> None:
        policy = await self._settings.get_setting("notification.policy.system_alerts")
        value = policy["value"]
        policy_key = (
            "recovered" if recovered else AlertSeverity(alert.severity).value.lower()
        )
        selected = value.get(policy_key, []) if value.get("enabled", True) else []
        statuses = {
            item["key"]: item for item in await self._settings.secret_statuses()
        }
        targets = []
        for channel_name in selected:
            channel = DeliveryChannel(channel_name)
            if _is_failed_channel(alert, channel):
                continue
            config = await self._settings.get_setting(_CONFIG_KEYS[channel])
            secret = statuses[_SECRET_KEYS[channel]]
            if config["value"]["enabled"] and secret["configured"]:
                targets.append(
                    ChannelDeliveryTarget(
                        channel,
                        config["version"],
                        target_fingerprint(channel, config, secret),
                    )
                )
        notice = "recovered" if recovered else "opened"
        await self._notifications.publish(
            PublishNotification(
                event_type="system.recovered"
                if recovered
                else _TEMPLATES[AlertSeverity(alert.severity)],
                business_event_type=f"alert.{notice}",
                business_event_id=f"{alert.id}:{alert.version}:{notice}",
                business_object_type="system_alert",
                business_object_id=str(alert.id),
                severity=alert.severity,
                template_variables={
                    "alert_type": alert.alert_type,
                    "message": alert.summary,
                },
                template_version="v1",
                targets=tuple(targets),
                idempotency_key=f"alert-notice:{alert.id}:{alert.version}:{notice}",
                request_id=request_id,
            )
        )

    async def publish_daily_unresolved(
        self,
        alert,
        *,
        reminder_date: date,
        request_id: str,
    ) -> None:
        policy = await self._settings.get_setting("notification.policy.system_alerts")
        value = policy["value"]
        selected = (
            value.get("daily_unresolved", []) if value.get("enabled", True) else []
        )
        statuses = {
            item["key"]: item for item in await self._settings.secret_statuses()
        }
        targets = []
        for channel_name in selected:
            channel = DeliveryChannel(channel_name)
            if _is_failed_channel(alert, channel):
                continue
            config = await self._settings.get_setting(_CONFIG_KEYS[channel])
            secret = statuses[_SECRET_KEYS[channel]]
            if config["value"]["enabled"] and secret["configured"]:
                targets.append(
                    ChannelDeliveryTarget(
                        channel,
                        config["version"],
                        target_fingerprint(channel, config, secret),
                    )
                )
        await self._notifications.publish(
            PublishNotification(
                event_type=_TEMPLATES[AlertSeverity(alert.severity)],
                business_event_type="alert.daily_unresolved",
                business_event_id=f"{alert.id}:{reminder_date.isoformat()}",
                business_object_type="system_alert",
                business_object_id=str(alert.id),
                severity=alert.severity,
                template_variables={
                    "alert_type": alert.alert_type,
                    "message": alert.summary,
                },
                template_version="v1",
                targets=tuple(targets),
                idempotency_key=(
                    f"alert-daily:{alert.id}:{reminder_date.isoformat()}"
                ),
                request_id=request_id,
            )
        )


def _is_failed_channel(alert, channel: DeliveryChannel) -> bool:
    if alert.alert_type not in {
        "NOTIFICATION_CHANNEL_FAILED",
        "NOTIFICATION_CHANNEL_DEGRADED",
    }:
        return False
    if alert.object_type != "notification_channel":
        return False
    return str(alert.object_id).upper() == channel.value
