import importlib
import importlib.util
from datetime import UTC, datetime, timedelta

import pytest


def load_delivery():
    module_name = "long_invest.modules.notifications.delivery"
    assert importlib.util.find_spec(module_name) is not None, (
        "notification delivery decisions are not implemented"
    )
    return importlib.import_module(module_name)


def test_temporary_failures_use_five_retries_and_stop_after_six_requests() -> None:
    delivery = load_delivery()
    policy = delivery.RetryPolicy()

    delays = [
        policy.decide(
            outcome=delivery.DeliveryOutcome.TEMPORARY_FAILURE,
            request_count=request_count,
            unknown_compensation_count=0,
        ).delay_seconds
        for request_count in range(1, 6)
    ]

    assert delays == [5, 30, 120, 600, 1800]
    exhausted = policy.decide(
        outcome=delivery.DeliveryOutcome.TEMPORARY_FAILURE,
        request_count=6,
        unknown_compensation_count=0,
    )
    assert exhausted.action is delivery.DeliveryAction.FAIL
    assert exhausted.reason == "MAX_REQUESTS_EXHAUSTED"


def test_permanent_failure_never_retries() -> None:
    delivery = load_delivery()

    decision = delivery.RetryPolicy().decide(
        outcome=delivery.DeliveryOutcome.PERMANENT_FAILURE,
        request_count=1,
        unknown_compensation_count=0,
    )

    assert decision.action is delivery.DeliveryAction.FAIL
    assert decision.delay_seconds is None


def test_unknown_outcome_allows_only_one_automatic_compensation() -> None:
    delivery = load_delivery()
    policy = delivery.RetryPolicy()

    first = policy.decide(
        outcome=delivery.DeliveryOutcome.OUTCOME_UNKNOWN,
        request_count=1,
        unknown_compensation_count=0,
    )
    second = policy.decide(
        outcome=delivery.DeliveryOutcome.OUTCOME_UNKNOWN,
        request_count=2,
        unknown_compensation_count=1,
    )

    assert first.action is delivery.DeliveryAction.RETRY
    assert first.consume_unknown_compensation is True
    assert second.action is delivery.DeliveryAction.KEEP_UNKNOWN
    assert second.consume_unknown_compensation is False


@pytest.mark.parametrize(
    ("request_count", "unknown_compensation_count"),
    [(0, 0), (7, 0), (1, -1), (1, 2)],
)
def test_retry_policy_rejects_invalid_attempt_counters(
    request_count: int,
    unknown_compensation_count: int,
) -> None:
    delivery = load_delivery()

    with pytest.raises(ValueError, match="counter"):
        delivery.RetryPolicy().decide(
            outcome=delivery.DeliveryOutcome.TEMPORARY_FAILURE,
            request_count=request_count,
            unknown_compensation_count=unknown_compensation_count,
        )


def test_event_status_aggregates_channels_without_cross_channel_retries() -> None:
    delivery = load_delivery()

    statuses = {
        delivery.DeliveryChannel.WECOM: delivery.ChannelDeliveryStatus.SENT,
        delivery.DeliveryChannel.EMAIL: delivery.ChannelDeliveryStatus.FAILED,
    }

    assert delivery.aggregate_event_status(statuses.values()) == "PARTIAL"
    assert delivery.channels_requiring_work(statuses) == {
        delivery.DeliveryChannel.EMAIL
    }


def test_event_is_not_partial_while_another_channel_is_still_retrying() -> None:
    delivery = load_delivery()

    statuses = (
        delivery.ChannelDeliveryStatus.SENT,
        delivery.ChannelDeliveryStatus.RETRY_WAIT,
    )

    assert delivery.aggregate_event_status(statuses) == "DISPATCHED"


def test_event_is_suppressed_when_all_channels_are_skipped() -> None:
    delivery = load_delivery()

    statuses = (
        delivery.ChannelDeliveryStatus.SKIPPED_INELIGIBLE,
        delivery.ChannelDeliveryStatus.SKIPPED_DISABLED,
    )

    assert delivery.aggregate_event_status(statuses) == "SUPPRESSED"


def test_circuit_opens_after_three_failures_and_uses_cooling_ladder() -> None:
    delivery = load_delivery()
    now = datetime(2026, 7, 14, tzinfo=UTC)
    snapshot = delivery.CircuitSnapshot.closed()

    for _ in range(3):
        snapshot = delivery.record_circuit_failure(snapshot, now=now)

    assert snapshot.state is delivery.CircuitState.OPEN
    assert snapshot.retry_at == now + timedelta(seconds=60)

    half_open = delivery.enter_half_open(snapshot, now=snapshot.retry_at)
    reopened = delivery.record_circuit_failure(half_open, now=snapshot.retry_at)
    assert reopened.state is delivery.CircuitState.OPEN
    assert reopened.retry_at == snapshot.retry_at + timedelta(seconds=180)


def test_successful_half_open_probe_recovers_only_that_circuit() -> None:
    delivery = load_delivery()
    now = datetime(2026, 7, 14, tzinfo=UTC)
    snapshot = delivery.CircuitSnapshot(
        state=delivery.CircuitState.HALF_OPEN,
        consecutive_failures=3,
        cooldown_level=2,
        retry_at=None,
    )

    recovered = delivery.record_circuit_success(snapshot)

    assert recovered == delivery.CircuitSnapshot.closed()
    assert delivery.circuit_allows_request(recovered, now=now) is True


def test_open_circuit_blocks_requests_until_a_half_open_probe_is_granted() -> None:
    delivery = load_delivery()
    now = datetime(2026, 7, 14, tzinfo=UTC)
    snapshot = delivery.CircuitSnapshot(
        state=delivery.CircuitState.OPEN,
        consecutive_failures=3,
        cooldown_level=0,
        retry_at=now + timedelta(seconds=60),
    )

    assert delivery.circuit_allows_request(snapshot, now=now) is False
    assert (
        delivery.circuit_allows_request(
            snapshot,
            now=now + timedelta(seconds=60),
        )
        is False
    )


def test_circuit_isolated_key_includes_channel_and_instance() -> None:
    delivery = load_delivery()

    wecom = delivery.CircuitKey(
        channel=delivery.DeliveryChannel.WECOM,
        instance="primary",
    )
    email = delivery.CircuitKey(
        channel=delivery.DeliveryChannel.EMAIL,
        instance="primary",
    )

    assert wecom != email
    assert str(wecom) == "WECOM:primary"
