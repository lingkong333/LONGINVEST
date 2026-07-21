from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from long_invest.modules.signals.contracts import (
    EvaluationReason,
    NotificationClass,
    SignalInput,
    SignalZone,
)
from long_invest.modules.signals.state_machine import (
    base_zone,
    hysteresis_buffer,
    next_zone,
    notification_class,
    should_create_event,
)
from long_invest.modules.targets.contracts import TargetValues


@pytest.fixture
def targets() -> TargetValues:
    return TargetValues(
        low_strong=Decimal("8.00"),
        low_watch=Decimal("9.00"),
        high_watch=Decimal("12.00"),
        high_strong=Decimal("13.00"),
    )


def signal_input(
    price: str,
    targets: TargetValues,
    *,
    reason: EvaluationReason = EvaluationReason.SCHEDULED_QUOTE,
    ratio: str = "0.02",
    minimum: str = "0.02",
) -> SignalInput:
    return SignalInput(
        subscription_id=UUID("10000000-0000-0000-0000-000000000001"),
        security_id=UUID("20000000-0000-0000-0000-000000000001"),
        symbol="600000.SH",
        security_name="浦发银行",
        subscription_version=1,
        price=Decimal(price),
        price_at=datetime(2026, 7, 17, 15, tzinfo=UTC),
        price_version=1,
        target_revision_id=UUID("30000000-0000-0000-0000-000000000001"),
        target_version=1,
        target_date=date(2026, 7, 17),
        targets=targets,
        position_version=0,
        hysteresis_ratio=Decimal(ratio),
        hysteresis_min=Decimal(minimum),
        reason=reason,
        idempotency_key=f"state-machine-{price}-{reason.value}",
        request_id="state-machine-test",
    )


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("8.00", SignalZone.STRONG_LOW),
        ("8.01", SignalZone.LOW),
        ("9.00", SignalZone.LOW),
        ("9.01", SignalZone.NORMAL),
        ("11.99", SignalZone.NORMAL),
        ("12.00", SignalZone.HIGH),
        ("12.99", SignalZone.HIGH),
        ("13.00", SignalZone.STRONG_HIGH),
    ],
)
def test_base_zone_has_all_eight_boundary_cases(
    price: str, expected: SignalZone, targets: TargetValues
) -> None:
    assert base_zone(Decimal(price), targets) is expected


@pytest.mark.parametrize("price", ["0", "-0.01", "NaN", "Infinity", "-Infinity"])
def test_base_zone_rejects_invalid_price(
    price: str, targets: TargetValues
) -> None:
    with pytest.raises(ValueError, match="price"):
        base_zone(Decimal(price), targets)


def test_hysteresis_buffer_uses_larger_of_ratio_and_minimum() -> None:
    assert hysteresis_buffer(
        Decimal("10"), Decimal("0.001"), Decimal("0.02")
    ) == Decimal("0.02")
    assert hysteresis_buffer(
        Decimal("10"), Decimal("0.05"), Decimal("0.02")
    ) == Decimal("0.50")


@pytest.mark.parametrize("target", ["0", "-0.01"])
def test_hysteresis_buffer_rejects_non_positive_target(target: str) -> None:
    with pytest.raises(ValueError, match="hysteresis"):
        hysteresis_buffer(Decimal(target), Decimal("0.02"), Decimal("0.02"))


def test_hysteresis_buffer_converts_finite_overflow_to_value_error() -> None:
    with pytest.raises(ValueError, match="hysteresis"):
        hysteresis_buffer(
            Decimal("10"), Decimal("1e999999"), Decimal("0.02")
        )


def test_hysteresis_buffer_accepts_a_very_large_finite_minimum() -> None:
    minimum = Decimal("1e999999")

    assert hysteresis_buffer(Decimal("10"), Decimal("0.02"), minimum) == minimum


@pytest.mark.parametrize(
    ("target", "ratio", "minimum"),
    [
        ("10", "-0.01", "0.02"),
        ("10", "0.01", "-0.02"),
        ("10", "NaN", "0.02"),
        ("10", "Infinity", "0.02"),
        ("10", "0.01", "NaN"),
        ("10", "0.01", "Infinity"),
        ("NaN", "0.01", "0.02"),
        ("Infinity", "0.01", "0.02"),
    ],
)
def test_hysteresis_buffer_rejects_negative_or_non_finite_inputs(
    target: str, ratio: str, minimum: str
) -> None:
    with pytest.raises(ValueError, match="hysteresis"):
        hysteresis_buffer(Decimal(target), Decimal(ratio), Decimal(minimum))


@pytest.mark.parametrize(
    ("current", "equal_price", "beyond_price", "expected"),
    [
        (SignalZone.LOW, "9.18", "9.180001", SignalZone.NORMAL),
        (SignalZone.STRONG_LOW, "8.16", "8.160001", SignalZone.LOW),
        (SignalZone.HIGH, "11.76", "11.759999", SignalZone.NORMAL),
        (SignalZone.STRONG_HIGH, "12.74", "12.739999", SignalZone.HIGH),
    ],
)
def test_exit_buffer_equality_holds_and_strict_crossing_exits(
    current: SignalZone,
    equal_price: str,
    beyond_price: str,
    expected: SignalZone,
    targets: TargetValues,
) -> None:
    assert next_zone(current, signal_input(equal_price, targets)) is current
    assert next_zone(current, signal_input(beyond_price, targets)) is expected


@pytest.mark.parametrize(
    ("current", "price", "expected"),
    [
        (SignalZone.LOW, "8.00", SignalZone.STRONG_LOW),
        (SignalZone.HIGH, "13.00", SignalZone.STRONG_HIGH),
        (SignalZone.NORMAL, "9.00", SignalZone.LOW),
        (SignalZone.NORMAL, "12.00", SignalZone.HIGH),
    ],
)
def test_entering_a_new_zone_uses_the_formal_target_line(
    current: SignalZone,
    price: str,
    expected: SignalZone,
    targets: TargetValues,
) -> None:
    assert next_zone(current, signal_input(price, targets)) is expected


@pytest.mark.parametrize(
    ("current", "price", "expected"),
    [
        (SignalZone.LOW, "13.00", SignalZone.STRONG_HIGH),
        (SignalZone.HIGH, "8.00", SignalZone.STRONG_LOW),
        (SignalZone.STRONG_LOW, "13.00", SignalZone.STRONG_HIGH),
        (SignalZone.STRONG_HIGH, "8.00", SignalZone.STRONG_LOW),
    ],
)
def test_crossing_multiple_zones_goes_directly_to_the_base_zone(
    current: SignalZone,
    price: str,
    expected: SignalZone,
    targets: TargetValues,
) -> None:
    assert next_zone(current, signal_input(price, targets)) is expected


@pytest.mark.parametrize(
    ("current", "price", "ratio", "minimum", "expected"),
    [
        (SignalZone.LOW, "12.50", "10", "0.02", SignalZone.HIGH),
        (SignalZone.LOW, "13.00", "0.02", "100", SignalZone.STRONG_HIGH),
        (SignalZone.HIGH, "8.50", "10", "0.02", SignalZone.LOW),
        (SignalZone.HIGH, "8.00", "0.02", "100", SignalZone.STRONG_LOW),
        (SignalZone.STRONG_LOW, "10.00", "10", "0.02", SignalZone.NORMAL),
        (SignalZone.STRONG_LOW, "12.50", "0.02", "100", SignalZone.HIGH),
        (
            SignalZone.STRONG_LOW,
            "13.00",
            "10",
            "0.02",
            SignalZone.STRONG_HIGH,
        ),
        (SignalZone.STRONG_HIGH, "10.00", "10", "0.02", SignalZone.NORMAL),
        (SignalZone.STRONG_HIGH, "8.50", "0.02", "100", SignalZone.LOW),
        (
            SignalZone.STRONG_HIGH,
            "8.00",
            "10",
            "0.02",
            SignalZone.STRONG_LOW,
        ),
    ],
)
def test_large_buffers_only_delay_adjacent_exits(
    current: SignalZone,
    price: str,
    ratio: str,
    minimum: str,
    expected: SignalZone,
    targets: TargetValues,
) -> None:
    input_value = signal_input(
        price, targets, ratio=ratio, minimum=minimum
    )

    assert next_zone(current, input_value) is expected


def test_next_zone_converts_finite_buffer_overflow_to_value_error(
    targets: TargetValues,
) -> None:
    input_value = signal_input("9.10", targets, ratio="9e999999")

    with pytest.raises(ValueError, match="hysteresis"):
        next_zone(SignalZone.LOW, input_value)


def test_next_zone_accepts_a_very_large_finite_minimum(
    targets: TargetValues,
) -> None:
    input_value = signal_input("9.10", targets, minimum="1e999999")

    assert next_zone(SignalZone.LOW, input_value) is SignalZone.LOW


@pytest.mark.parametrize(
    "reason", [EvaluationReason.TARGET_ACTIVATED, EvaluationReason.STATE_RESET]
)
def test_reclassification_reasons_bypass_old_hysteresis(
    reason: EvaluationReason, targets: TargetValues
) -> None:
    input_value = signal_input("9.10", targets, reason=reason)

    assert next_zone(SignalZone.LOW, input_value) is SignalZone.NORMAL


@pytest.mark.parametrize(
    "reason",
    [
        EvaluationReason.SCHEDULED_QUOTE,
        EvaluationReason.MANUAL_CHECK,
        EvaluationReason.POSITION_BECAME_HOLDING,
        EvaluationReason.DATA_CORRECTION,
        EvaluationReason.RECOVERY_REEVALUATION,
    ],
)
def test_other_reasons_keep_old_hysteresis(
    reason: EvaluationReason, targets: TargetValues
) -> None:
    input_value = signal_input("9.10", targets, reason=reason)

    assert next_zone(SignalZone.LOW, input_value) is SignalZone.LOW


def test_unknown_uses_base_zone(targets: TargetValues) -> None:
    assert next_zone(
        SignalZone.UNKNOWN, signal_input("12.00", targets)
    ) is SignalZone.HIGH


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (SignalZone.UNKNOWN, SignalZone.NORMAL, None),
        (SignalZone.UNKNOWN, SignalZone.LOW, NotificationClass.LOW),
        (SignalZone.NORMAL, SignalZone.STRONG_LOW, NotificationClass.LOW),
        (SignalZone.LOW, SignalZone.NORMAL, NotificationClass.LOW_CLEARED),
        (SignalZone.NORMAL, SignalZone.STRONG_HIGH, NotificationClass.HIGH),
        (SignalZone.HIGH, SignalZone.NORMAL, NotificationClass.HIGH_CLEARED),
        (SignalZone.HIGH, SignalZone.LOW, NotificationClass.LOW),
        (SignalZone.LOW, SignalZone.HIGH, NotificationClass.HIGH),
        (SignalZone.NORMAL, SignalZone.NORMAL, None),
    ],
)
def test_notification_class_follows_the_arrival_zone(
    before: SignalZone,
    after: SignalZone,
    expected: NotificationClass | None,
) -> None:
    assert notification_class(before, after) is expected


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (SignalZone.UNKNOWN, SignalZone.NORMAL, False),
        (SignalZone.UNKNOWN, SignalZone.LOW, True),
        (SignalZone.NORMAL, SignalZone.HIGH, True),
        (SignalZone.HIGH, SignalZone.LOW, True),
        (SignalZone.LOW, SignalZone.LOW, False),
    ],
)
def test_event_is_created_only_for_real_non_baseline_changes(
    before: SignalZone, after: SignalZone, expected: bool
) -> None:
    assert should_create_event(before, after) is expected
