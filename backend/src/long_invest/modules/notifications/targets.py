from __future__ import annotations

import hashlib
import json
from typing import Any

from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.service import ChannelDeliveryTarget
from long_invest.modules.settings.service import transactional_settings_service

_CHANNEL_SETTING = {
    DeliveryChannel.WECOM: "notification.channel.wecom",
    DeliveryChannel.EMAIL: "notification.channel.email",
}
_CHANNEL_SECRET = {
    DeliveryChannel.WECOM: "notification.wecom.webhook",
    DeliveryChannel.EMAIL: "notification.email.password",
}


class DynamicNotificationTargetResolver:
    def __init__(self, session: Any) -> None:
        self._settings = transactional_settings_service(session)

    async def resolve_targets(
        self, notification: Any
    ) -> tuple[ChannelDeliveryTarget, ...]:
        global_policy = await self._settings.get_setting("notification.policy.global")
        signal_policy = await self._settings.get_setting("notification.policy.signals")
        if (
            not global_policy["value"]["enabled"]
            or not signal_policy["value"]["enabled"]
        ):
            return ()
        if notification.notification_mode == "CUSTOM":
            selected = notification.notification_channels
        else:
            selected = (
                signal_policy["value"]["channels"] or global_policy["value"]["channels"]
            )
        secret_statuses = {
            item["key"]: item for item in await self._settings.secret_statuses()
        }
        targets: list[ChannelDeliveryTarget] = []
        for channel_name in selected:
            channel = DeliveryChannel(channel_name)
            config = await self._settings.get_setting(_CHANNEL_SETTING[channel])
            secret = secret_statuses[_CHANNEL_SECRET[channel]]
            if not config["value"]["enabled"] or not secret["configured"]:
                continue
            fingerprint = target_fingerprint(channel, config, secret)
            targets.append(
                ChannelDeliveryTarget(
                    channel=channel,
                    config_version=config["version"],
                    target_fingerprint=fingerprint,
                )
            )
        return tuple(targets)


def target_fingerprint(
    channel: DeliveryChannel,
    config: dict[str, Any],
    secret: dict[str, Any],
) -> str:
    fingerprint_data = {
        "channel": channel.value,
        "config": config["value"],
        "secret_fingerprint": secret["fingerprint"],
        "secret_version": secret["version"],
    }
    return hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True).encode()
    ).hexdigest()[:32]
