from dataclasses import dataclass
from enum import StrEnum

from long_invest.modules.notifications.contracts import (
    DeliveryChannel as NotificationChannel,
)
from long_invest.modules.notifications.contracts import (
    SystemAlertNoticeKind,
    SystemAlertSeverity,
)


class PolicyMode(StrEnum):
    INHERIT = "INHERIT"
    CUSTOM = "CUSTOM"


class PolicySource(StrEnum):
    SUBSCRIPTION = "SUBSCRIPTION"
    SIGNAL_TYPE = "SIGNAL_TYPE"
    GLOBAL = "GLOBAL"
    SYSTEM_ALERT = "SYSTEM_ALERT"


class PolicyResolutionError(ValueError):
    code = "NOTIFICATION_POLICY_UNCONFIGURED"


@dataclass(frozen=True, slots=True)
class PolicySelection:
    channels: frozenset[NotificationChannel]

    def __post_init__(self) -> None:
        has_unknown = any(
            not isinstance(channel, NotificationChannel) for channel in self.channels
        )
        if has_unknown:
            raise ValueError("policy contains an unknown notification channel")

    @classmethod
    def web_only(cls) -> "PolicySelection":
        return cls(frozenset())

    @classmethod
    def wecom_only(cls) -> "PolicySelection":
        return cls(frozenset({NotificationChannel.WECOM}))

    @classmethod
    def email_only(cls) -> "PolicySelection":
        return cls(frozenset({NotificationChannel.EMAIL}))

    @classmethod
    def both(cls) -> "PolicySelection":
        return cls(frozenset(NotificationChannel))


@dataclass(frozen=True, slots=True)
class PolicyOverride:
    mode: PolicyMode
    selection: PolicySelection | None = None

    def __post_init__(self) -> None:
        if (self.mode is PolicyMode.CUSTOM) != (self.selection is not None):
            raise ValueError("CUSTOM requires a selection and INHERIT forbids one")

    @classmethod
    def inherit(cls) -> "PolicyOverride":
        return cls(PolicyMode.INHERIT)

    @classmethod
    def custom(cls, selection: PolicySelection) -> "PolicyOverride":
        return cls(PolicyMode.CUSTOM, selection)


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    channels: frozenset[NotificationChannel]
    source: PolicySource


@dataclass(frozen=True, slots=True)
class SystemAlertPolicy:
    warning: PolicySelection
    error: PolicySelection
    critical: PolicySelection
    recovered: PolicySelection
    daily_unresolved: PolicySelection


def _custom_selection(override: PolicyOverride | None) -> PolicySelection | None:
    if override is None or override.mode is PolicyMode.INHERIT:
        return None
    return override.selection


def resolve_signal_policy(
    *,
    subscription: PolicyOverride | None,
    signal_type: PolicyOverride | None,
    global_default: PolicySelection | None,
) -> ResolvedPolicy:
    subscription_selection = _custom_selection(subscription)
    if subscription_selection is not None:
        return ResolvedPolicy(
            subscription_selection.channels,
            PolicySource.SUBSCRIPTION,
        )

    signal_selection = _custom_selection(signal_type)
    if signal_selection is not None:
        return ResolvedPolicy(signal_selection.channels, PolicySource.SIGNAL_TYPE)

    if global_default is None:
        raise PolicyResolutionError(
            "an explicit global notification policy is required"
        )
    return ResolvedPolicy(global_default.channels, PolicySource.GLOBAL)


def resolve_system_alert_policy(
    *,
    policy: SystemAlertPolicy,
    severity: SystemAlertSeverity,
    notice_kind: SystemAlertNoticeKind,
) -> ResolvedPolicy:
    if notice_kind is SystemAlertNoticeKind.RECOVERED:
        selection = policy.recovered
    elif notice_kind is SystemAlertNoticeKind.DAILY_UNRESOLVED:
        selection = policy.daily_unresolved
    else:
        selection = {
            SystemAlertSeverity.WARNING: policy.warning,
            SystemAlertSeverity.ERROR: policy.error,
            SystemAlertSeverity.CRITICAL: policy.critical,
        }[severity]
    return ResolvedPolicy(selection.channels, PolicySource.SYSTEM_ALERT)
