from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.signals.contracts import (
    EvaluationReason,
    EvaluationResult,
    NotificationClass,
    SignalInput,
    SignalZone,
)


def _signal_input(**overrides: object) -> SignalInput:
    values: dict[str, object] = {
        "subscription_id": uuid4(),
        "symbol": "600000.SH",
        "subscription_version": 1,
        "price": "10.01",
        "price_at": datetime(2026, 7, 17, 9, 30, tzinfo=UTC),
        "price_version": 1,
        "target_revision_id": uuid4(),
        "target_version": 1,
        "position_version": 1,
        "hysteresis_ratio": "0.02",
        "hysteresis_min": "0.02",
        "reason": EvaluationReason.SCHEDULED_QUOTE,
        "idempotency_key": " signal-1 ",
    }
    values.update(overrides)
    return SignalInput(**values)


def test_signal_enums_are_exact() -> None:
    assert {item.value for item in SignalZone} == {
        "UNKNOWN",
        "STRONG_LOW",
        "LOW",
        "NORMAL",
        "HIGH",
        "STRONG_HIGH",
    }
    assert {item.value for item in EvaluationReason} == {
        "SCHEDULED_QUOTE",
        "MANUAL_CHECK",
        "TARGET_ACTIVATED",
        "POSITION_BECAME_HOLDING",
        "DATA_CORRECTION",
        "STATE_RESET",
        "RECOVERY_REEVALUATION",
    }
    assert {item.value for item in EvaluationResult} == {
        "APPLIED",
        "UNCHANGED",
        "SKIPPED",
        "SUPERSEDED",
    }
    assert {item.value for item in NotificationClass} == {
        "LOW",
        "LOW_CLEARED",
        "HIGH",
        "HIGH_CLEARED",
    }


def test_signal_input_accepts_valid_values_and_is_frozen() -> None:
    value = _signal_input()
    assert value.price == Decimal("10.01")
    assert value.idempotency_key == "signal-1"
    with pytest.raises(ValidationError):
        value.price = Decimal("1")
    with pytest.raises(ValidationError):
        SignalInput.model_validate(value.model_dump() | {"extra": True})


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "-Infinity"])
def test_signal_input_rejects_invalid_price(price: str) -> None:
    with pytest.raises(ValidationError):
        _signal_input(price=price)


@pytest.mark.parametrize(
    "field", ["subscription_version", "price_version", "target_version"]
)
def test_signal_input_rejects_non_positive_versions(field: str) -> None:
    with pytest.raises(ValidationError):
        _signal_input(**{field: 0})


@pytest.mark.parametrize("field", ["hysteresis_ratio", "hysteresis_min"])
def test_signal_input_rejects_negative_hysteresis(field: str) -> None:
    with pytest.raises(ValidationError):
        _signal_input(**{field: "-0.01"})


def test_signal_input_requires_aware_time_and_valid_symbol() -> None:
    with pytest.raises(ValidationError):
        _signal_input(price_at=datetime(2026, 7, 17, 9, 30))
    with pytest.raises(ValidationError):
        _signal_input(symbol="600000")
