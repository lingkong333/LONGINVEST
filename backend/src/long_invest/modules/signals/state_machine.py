from __future__ import annotations

from decimal import Decimal

from long_invest.modules.signals.contracts import (
    EvaluationReason,
    NotificationClass,
    SignalInput,
    SignalZone,
)
from long_invest.modules.targets.contracts import TargetValues

_LOW_ZONES = frozenset({SignalZone.LOW, SignalZone.STRONG_LOW})
_HIGH_ZONES = frozenset({SignalZone.HIGH, SignalZone.STRONG_HIGH})
_RECLASSIFICATION_REASONS = frozenset(
    {EvaluationReason.TARGET_ACTIVATED, EvaluationReason.STATE_RESET}
)


def base_zone(price: Decimal, targets: TargetValues) -> SignalZone:
    if not price.is_finite() or price <= 0:
        raise ValueError("price must be positive and finite")
    if price <= targets.low_strong:
        return SignalZone.STRONG_LOW
    if price <= targets.low_watch:
        return SignalZone.LOW
    if price < targets.high_watch:
        return SignalZone.NORMAL
    if price < targets.high_strong:
        return SignalZone.HIGH
    return SignalZone.STRONG_HIGH


def hysteresis_buffer(
    target: Decimal, ratio: Decimal, minimum: Decimal
) -> Decimal:
    if (
        not target.is_finite()
        or not ratio.is_finite()
        or not minimum.is_finite()
        or target < 0
        or ratio < 0
        or minimum < 0
    ):
        raise ValueError("hysteresis inputs must be finite and non-negative")
    return max(target * ratio, minimum)


def next_zone(current: SignalZone, signal_input: SignalInput) -> SignalZone:
    price = signal_input.price
    targets = signal_input.targets
    desired = base_zone(price, targets)

    if (
        current in {SignalZone.UNKNOWN, SignalZone.NORMAL}
        or signal_input.reason in _RECLASSIFICATION_REASONS
    ):
        return desired

    ratio = signal_input.hysteresis_ratio
    minimum = signal_input.hysteresis_min

    if current is SignalZone.LOW:
        if desired is SignalZone.STRONG_LOW:
            return desired
        boundary = targets.low_watch + hysteresis_buffer(
            targets.low_watch, ratio, minimum
        )
        return desired if price > boundary else current

    if current is SignalZone.STRONG_LOW:
        boundary = targets.low_strong + hysteresis_buffer(
            targets.low_strong, ratio, minimum
        )
        return desired if price > boundary else current

    if current is SignalZone.HIGH:
        if desired is SignalZone.STRONG_HIGH:
            return desired
        boundary = targets.high_watch - hysteresis_buffer(
            targets.high_watch, ratio, minimum
        )
        return desired if price < boundary else current

    if current is SignalZone.STRONG_HIGH:
        boundary = targets.high_strong - hysteresis_buffer(
            targets.high_strong, ratio, minimum
        )
        return desired if price < boundary else current

    return desired


def notification_class(
    before: SignalZone, after: SignalZone
) -> NotificationClass | None:
    if before is after or (before is SignalZone.UNKNOWN and after is SignalZone.NORMAL):
        return None
    if after in _LOW_ZONES:
        return NotificationClass.LOW
    if after in _HIGH_ZONES:
        return NotificationClass.HIGH
    if after is SignalZone.NORMAL and before in _LOW_ZONES:
        return NotificationClass.LOW_CLEARED
    if after is SignalZone.NORMAL and before in _HIGH_ZONES:
        return NotificationClass.HIGH_CLEARED
    return None


def should_create_event(before: SignalZone, after: SignalZone) -> bool:
    return before is not after and not (
        before is SignalZone.UNKNOWN and after is SignalZone.NORMAL
    )
