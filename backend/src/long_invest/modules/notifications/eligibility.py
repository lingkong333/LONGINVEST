from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from long_invest.modules.notifications.contracts import (
    SystemAlertNoticeKind,
    SystemAlertSeverity,
)


class NotificationKind(StrEnum):
    SIGNAL_LOW = "SIGNAL_LOW"
    SIGNAL_LOW_CLEARED = "SIGNAL_LOW_CLEARED"
    SIGNAL_HIGH = "SIGNAL_HIGH"
    SIGNAL_HIGH_CLEARED = "SIGNAL_HIGH_CLEARED"


_HIGH_KINDS = {
    NotificationKind.SIGNAL_HIGH,
    NotificationKind.SIGNAL_HIGH_CLEARED,
}


@dataclass(frozen=True, slots=True)
class EligibilityRequest:
    event_kind: NotificationKind
    expected_position_version: int | None = None


@dataclass(frozen=True, slots=True)
class EligibilitySnapshot:
    canceled: bool
    channel_enabled: bool
    subscription_enabled: bool
    is_holding: bool
    position_version: int


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    reason: str | None
    delivery_status: str | None


@dataclass(frozen=True, slots=True)
class SystemAlertEligibilityRequest:
    notice_kind: SystemAlertNoticeKind
    expected_alert_version: int
    expected_severity: SystemAlertSeverity


@dataclass(frozen=True, slots=True)
class SystemAlertEligibilitySnapshot:
    canceled: bool
    channel_enabled: bool
    resolved: bool
    alert_version: int
    severity: SystemAlertSeverity
    reminded_today: bool


def review_eligibility(
    request: EligibilityRequest,
    snapshot: EligibilitySnapshot,
) -> EligibilityDecision:
    if snapshot.canceled:
        return EligibilityDecision(False, "EVENT_CANCELED", "CANCELED")
    if not snapshot.channel_enabled:
        return EligibilityDecision(False, "CHANNEL_DISABLED", "SKIPPED_DISABLED")
    if not snapshot.subscription_enabled:
        return EligibilityDecision(
            False,
            "SUBSCRIPTION_DISABLED",
            "SKIPPED_INELIGIBLE",
        )
    if request.event_kind in _HIGH_KINDS:
        if not snapshot.is_holding:
            return EligibilityDecision(False, "NOT_HOLDING", "SKIPPED_INELIGIBLE")
        if (
            request.expected_position_version is not None
            and request.expected_position_version != snapshot.position_version
        ):
            return EligibilityDecision(
                False,
                "POSITION_VERSION_CHANGED",
                "SKIPPED_INELIGIBLE",
            )
    return EligibilityDecision(True, None, None)


def review_before_send(
    request: EligibilityRequest,
    current_snapshot: Callable[[EligibilityRequest], EligibilitySnapshot],
) -> EligibilityDecision:
    return review_eligibility(request, current_snapshot(request))


def review_system_alert_eligibility(
    request: SystemAlertEligibilityRequest,
    snapshot: SystemAlertEligibilitySnapshot,
) -> EligibilityDecision:
    if snapshot.canceled:
        return EligibilityDecision(False, "EVENT_CANCELED", "CANCELED")
    if not snapshot.channel_enabled:
        return EligibilityDecision(False, "CHANNEL_DISABLED", "SKIPPED_DISABLED")
    if request.expected_alert_version != snapshot.alert_version:
        return EligibilityDecision(
            False,
            "ALERT_VERSION_CHANGED",
            "SKIPPED_INELIGIBLE",
        )
    if request.expected_severity is not snapshot.severity:
        return EligibilityDecision(
            False,
            "ALERT_SEVERITY_CHANGED",
            "SKIPPED_INELIGIBLE",
        )
    if request.notice_kind is SystemAlertNoticeKind.RECOVERED:
        if snapshot.resolved:
            return EligibilityDecision(True, None, None)
        return EligibilityDecision(
            False,
            "ALERT_NOT_RESOLVED",
            "SKIPPED_INELIGIBLE",
        )
    if snapshot.resolved:
        return EligibilityDecision(
            False,
            "ALERT_ALREADY_RESOLVED",
            "SKIPPED_INELIGIBLE",
        )
    if (
        request.notice_kind is SystemAlertNoticeKind.DAILY_UNRESOLVED
        and snapshot.reminded_today
    ):
        return EligibilityDecision(
            False,
            "DAILY_REMINDER_ALREADY_SENT",
            "SKIPPED_INELIGIBLE",
        )
    return EligibilityDecision(True, None, None)
