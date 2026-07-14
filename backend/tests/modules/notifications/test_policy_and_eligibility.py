import importlib
import importlib.util
from dataclasses import fields
from inspect import signature

import pytest


def load_module(name: str):
    module_name = f"long_invest.modules.notifications.{name}"
    assert importlib.util.find_spec(module_name) is not None, (
        f"notification {name} capability is not implemented"
    )
    return importlib.import_module(module_name)


def test_signal_policy_uses_subscription_then_type_then_global_priority() -> None:
    policy = load_module("policy")

    global_default = policy.PolicySelection.email_only()
    signal_type = policy.PolicyOverride.custom(policy.PolicySelection.wecom_only())
    subscription = policy.PolicyOverride.custom(policy.PolicySelection.both())

    resolved = policy.resolve_signal_policy(
        subscription=subscription,
        signal_type=signal_type,
        global_default=global_default,
    )

    assert resolved.source is policy.PolicySource.SUBSCRIPTION
    assert resolved.channels == frozenset(
        {policy.NotificationChannel.WECOM, policy.NotificationChannel.EMAIL}
    )

    inherited = policy.resolve_signal_policy(
        subscription=policy.PolicyOverride.inherit(),
        signal_type=signal_type,
        global_default=global_default,
    )
    assert inherited.source is policy.PolicySource.SIGNAL_TYPE
    assert inherited.channels == frozenset({policy.NotificationChannel.WECOM})


def test_signal_policy_requires_an_explicit_global_choice() -> None:
    policy = load_module("policy")

    with pytest.raises(policy.PolicyResolutionError) as exc_info:
        policy.resolve_signal_policy(
            subscription=policy.PolicyOverride.inherit(),
            signal_type=policy.PolicyOverride.inherit(),
            global_default=None,
        )

    assert exc_info.value.code == "NOTIFICATION_POLICY_UNCONFIGURED"


def test_web_only_is_a_valid_explicit_policy() -> None:
    policy = load_module("policy")

    resolved = policy.resolve_signal_policy(
        subscription=None,
        signal_type=None,
        global_default=policy.PolicySelection.web_only(),
    )

    assert resolved.source is policy.PolicySource.GLOBAL
    assert resolved.channels == frozenset()


def test_policy_selection_rejects_an_unknown_channel() -> None:
    policy = load_module("policy")

    with pytest.raises(ValueError, match="channel"):
        policy.PolicySelection(frozenset({"SMS"}))


def test_high_signal_is_suppressed_by_pre_send_hook_when_not_holding() -> None:
    eligibility = load_module("eligibility")
    calls = []

    request = eligibility.EligibilityRequest(
        event_kind=eligibility.NotificationKind.SIGNAL_HIGH,
        expected_position_version=7,
    )

    def current_snapshot(received):
        calls.append(received)
        return eligibility.EligibilitySnapshot(
            canceled=False,
            channel_enabled=True,
            subscription_enabled=True,
            is_holding=False,
            position_version=7,
        )

    decision = eligibility.review_before_send(request, current_snapshot)

    assert calls == [request]
    assert decision.eligible is False
    assert decision.reason == "NOT_HOLDING"
    assert decision.delivery_status == "SKIPPED_INELIGIBLE"


def test_low_signal_does_not_require_a_position() -> None:
    eligibility = load_module("eligibility")
    request = eligibility.EligibilityRequest(
        event_kind=eligibility.NotificationKind.SIGNAL_LOW,
    )
    snapshot = eligibility.EligibilitySnapshot(
        canceled=False,
        channel_enabled=True,
        subscription_enabled=True,
        is_holding=False,
        position_version=11,
    )

    decision = eligibility.review_eligibility(request, snapshot)

    assert decision.eligible is True
    assert decision.reason is None


def test_emergency_pause_invalidates_a_pending_delivery() -> None:
    eligibility = load_module("eligibility")
    request = eligibility.EligibilityRequest(
        event_kind=eligibility.NotificationKind.SIGNAL_LOW,
    )
    snapshot = eligibility.EligibilitySnapshot(
        canceled=False,
        channel_enabled=True,
        subscription_enabled=False,
        is_holding=True,
        position_version=2,
    )

    decision = eligibility.review_eligibility(request, snapshot)

    assert decision.eligible is False
    assert decision.reason == "SUBSCRIPTION_DISABLED"


def test_system_alert_policy_has_independent_severity_and_lifecycle_channels() -> None:
    policy = load_module("policy")
    alert_policy = policy.SystemAlertPolicy(
        warning=policy.PolicySelection.web_only(),
        error=policy.PolicySelection.email_only(),
        critical=policy.PolicySelection.both(),
        recovered=policy.PolicySelection.wecom_only(),
        daily_unresolved=policy.PolicySelection.email_only(),
    )

    warning = policy.resolve_system_alert_policy(
        policy=alert_policy,
        severity=policy.SystemAlertSeverity.WARNING,
        notice_kind=policy.SystemAlertNoticeKind.OPENED,
    )
    critical = policy.resolve_system_alert_policy(
        policy=alert_policy,
        severity=policy.SystemAlertSeverity.CRITICAL,
        notice_kind=policy.SystemAlertNoticeKind.OPENED,
    )
    recovered = policy.resolve_system_alert_policy(
        policy=alert_policy,
        severity=policy.SystemAlertSeverity.ERROR,
        notice_kind=policy.SystemAlertNoticeKind.RECOVERED,
    )
    daily = policy.resolve_system_alert_policy(
        policy=alert_policy,
        severity=policy.SystemAlertSeverity.ERROR,
        notice_kind=policy.SystemAlertNoticeKind.DAILY_UNRESOLVED,
    )

    assert warning.channels == frozenset()
    assert critical.channels == frozenset(policy.NotificationChannel)
    assert recovered.channels == frozenset({policy.NotificationChannel.WECOM})
    assert daily.channels == frozenset({policy.NotificationChannel.EMAIL})
    assert (
        "subscription" not in signature(policy.resolve_system_alert_policy).parameters
    )


def test_system_alert_eligibility_snapshot_has_no_stock_subscription_facts() -> None:
    eligibility = load_module("eligibility")

    field_names = {
        field.name for field in fields(eligibility.SystemAlertEligibilitySnapshot)
    }

    assert "subscription_enabled" not in field_names
    assert "is_holding" not in field_names
    assert "position_version" not in field_names
    assert "SYSTEM_ALERT" not in eligibility.NotificationKind.__members__


def test_resolved_alert_sends_only_recovery_notice() -> None:
    eligibility = load_module("eligibility")
    snapshot = eligibility.SystemAlertEligibilitySnapshot(
        canceled=False,
        channel_enabled=True,
        resolved=True,
        alert_version=5,
        severity=eligibility.SystemAlertSeverity.ERROR,
        reminded_today=False,
    )

    opened = eligibility.review_system_alert_eligibility(
        eligibility.SystemAlertEligibilityRequest(
            notice_kind=eligibility.SystemAlertNoticeKind.OPENED,
            expected_alert_version=5,
            expected_severity=eligibility.SystemAlertSeverity.ERROR,
        ),
        snapshot,
    )
    recovered = eligibility.review_system_alert_eligibility(
        eligibility.SystemAlertEligibilityRequest(
            notice_kind=eligibility.SystemAlertNoticeKind.RECOVERED,
            expected_alert_version=5,
            expected_severity=eligibility.SystemAlertSeverity.ERROR,
        ),
        snapshot,
    )

    assert opened.eligible is False
    assert opened.reason == "ALERT_ALREADY_RESOLVED"
    assert recovered.eligible is True


def test_daily_unresolved_alert_is_suppressed_after_today_reminder() -> None:
    eligibility = load_module("eligibility")
    request = eligibility.SystemAlertEligibilityRequest(
        notice_kind=eligibility.SystemAlertNoticeKind.DAILY_UNRESOLVED,
        expected_alert_version=8,
        expected_severity=eligibility.SystemAlertSeverity.CRITICAL,
    )
    snapshot = eligibility.SystemAlertEligibilitySnapshot(
        canceled=False,
        channel_enabled=True,
        resolved=False,
        alert_version=8,
        severity=eligibility.SystemAlertSeverity.CRITICAL,
        reminded_today=True,
    )

    decision = eligibility.review_system_alert_eligibility(request, snapshot)

    assert decision.eligible is False
    assert decision.reason == "DAILY_REMINDER_ALREADY_SENT"


def test_changed_alert_version_invalidates_old_pending_notice() -> None:
    eligibility = load_module("eligibility")
    request = eligibility.SystemAlertEligibilityRequest(
        notice_kind=eligibility.SystemAlertNoticeKind.OPENED,
        expected_alert_version=3,
        expected_severity=eligibility.SystemAlertSeverity.ERROR,
    )
    snapshot = eligibility.SystemAlertEligibilitySnapshot(
        canceled=False,
        channel_enabled=True,
        resolved=False,
        alert_version=4,
        severity=eligibility.SystemAlertSeverity.CRITICAL,
        reminded_today=False,
    )

    decision = eligibility.review_system_alert_eligibility(request, snapshot)

    assert decision.eligible is False
    assert decision.reason == "ALERT_VERSION_CHANGED"
