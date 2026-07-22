import asyncio
from types import SimpleNamespace

from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.targets import DynamicNotificationTargetResolver


class FakeSettings:
    def __init__(self, *, global_enabled=True, signal_channels=None, configured=True):
        self.global_enabled = global_enabled
        self.signal_channels = signal_channels or []
        self.configured = configured

    async def get_setting(self, key):
        values = {
            "notification.policy.global": {
                "value": {"enabled": self.global_enabled, "channels": ["WECOM"]},
                "version": 1,
            },
            "notification.policy.signals": {
                "value": {"enabled": True, "channels": self.signal_channels},
                "version": 2,
            },
            "notification.channel.wecom": {
                "value": {"enabled": True, "timeout_seconds": 5},
                "version": 3,
            },
            "notification.channel.email": {
                "value": {"enabled": True, "smtp_host": "smtp.example.com"},
                "version": 4,
            },
        }
        return values[key]

    async def secret_statuses(self):
        return [
            {
                "key": "notification.wecom.webhook",
                "configured": self.configured,
                "version": 5,
                "fingerprint": "wecom-fingerprint",
            },
            {
                "key": "notification.email.password",
                "configured": self.configured,
                "version": 6,
                "fingerprint": "email-fingerprint",
            },
        ]


def resolver(settings):
    subject = DynamicNotificationTargetResolver.__new__(
        DynamicNotificationTargetResolver
    )
    subject._settings = settings
    return subject


def notification(*, mode="INHERIT", channels=()):
    return SimpleNamespace(
        notification_mode=mode,
        notification_channels=channels,
    )


def test_signal_policy_falls_back_to_global_and_freezes_target() -> None:
    targets = asyncio.run(resolver(FakeSettings()).resolve_targets(notification()))

    assert len(targets) == 1
    assert targets[0].channel is DeliveryChannel.WECOM
    assert targets[0].config_version == 3
    assert len(targets[0].target_fingerprint) == 32


def test_signal_policy_can_override_global_channels() -> None:
    targets = asyncio.run(
        resolver(FakeSettings(signal_channels=["EMAIL"])).resolve_targets(
            notification()
        )
    )

    assert [item.channel for item in targets] == [DeliveryChannel.EMAIL]


def test_disabled_policy_or_missing_secret_produces_web_only_event() -> None:
    disabled = asyncio.run(
        resolver(FakeSettings(global_enabled=False)).resolve_targets(notification())
    )
    missing_secret = asyncio.run(
        resolver(FakeSettings(configured=False)).resolve_targets(notification())
    )

    assert disabled == ()
    assert missing_secret == ()


def test_subscription_custom_channels_override_signal_and_global_defaults() -> None:
    selected = asyncio.run(
        resolver(FakeSettings(signal_channels=["WECOM"])).resolve_targets(
            notification(mode="CUSTOM", channels=("EMAIL",))
        )
    )
    web_only = asyncio.run(
        resolver(FakeSettings(signal_channels=["WECOM"])).resolve_targets(
            notification(mode="CUSTOM", channels=())
        )
    )

    assert [item.channel for item in selected] == [DeliveryChannel.EMAIL]
    assert web_only == ()
