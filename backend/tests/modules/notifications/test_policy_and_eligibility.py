import importlib
import importlib.util

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
