from __future__ import annotations

from decimal import Decimal, DecimalException

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
        or target <= 0
        or ratio < 0
        or minimum < 0
    ):
        raise ValueError(
            "hysteresis target must be positive and inputs finite and non-negative"
        )
    try:
        scaled = target * ratio
    except DecimalException as exc:
        raise ValueError("hysteresis result must be finite") from exc
    if not scaled.is_finite():
        raise ValueError("hysteresis result must be finite")
    return max(scaled, minimum)


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
        if desired is SignalZone.NORMAL:
            boundary = targets.low_watch + hysteresis_buffer(
                targets.low_watch, ratio, minimum
            )
            return desired if price > boundary else current
        return desired

    if current is SignalZone.STRONG_LOW:
        if desired is SignalZone.LOW:
            boundary = targets.low_strong + hysteresis_buffer(
                targets.low_strong, ratio, minimum
            )
            return desired if price > boundary else current
        return desired

    if current is SignalZone.HIGH:
        if desired is SignalZone.NORMAL:
            boundary = targets.high_watch - hysteresis_buffer(
                targets.high_watch, ratio, minimum
            )
            return desired if price < boundary else current
        return desired

    if current is SignalZone.STRONG_HIGH:
        if desired is SignalZone.HIGH:
            boundary = targets.high_strong - hysteresis_buffer(
                targets.high_strong, ratio, minimum
            )
            return desired if price < boundary else current
        return desired

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
