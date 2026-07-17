from datetime import UTC, date, datetime
from decimal import Decimal
from inspect import signature
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.signals.contracts import (
    EvaluationReason,
    EvaluationResult,
    NotificationClass,
    PositionSnapshotPort,
    SignalEvaluationView,
    SignalEventView,
    SignalInput,
    SignalStateView,
    SignalZone,
)
from long_invest.modules.targets.contracts import TargetValues


def _signal_input(**overrides: object) -> SignalInput:
    values: dict[str, object] = {
        "subscription_id": uuid4(),
        "security_id": uuid4(),
        "symbol": "600000.SH",
        "subscription_version": 1,
        "price": "10.01",
        "price_at": datetime(2026, 7, 17, 9, 30, tzinfo=UTC),
        "price_version": 1,
        "target_revision_id": uuid4(),
        "target_version": 1,
        "target_date": date(2026, 7, 17),
        "targets": TargetValues(
            low_strong="8", low_watch="9", high_watch="11", high_strong="12"
        ),
        "quote_cycle_id": uuid4(),
        "quote_item_id": uuid4(),
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
    assert SignalEventView.model_fields["reason"].annotation is EvaluationReason


def test_signal_input_accepts_valid_values_and_is_frozen() -> None:
    value = _signal_input()
    assert value.price == Decimal("10.01")
    assert value.idempotency_key == "signal-1"
    assert value.targets.low_watch == Decimal("9.00")
    assert value.security_id is not None
    assert value.target_date == date(2026, 7, 17)
    assert value.quote_cycle_id is not None
    assert value.quote_item_id is not None
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


def test_signal_input_allows_position_version_zero_and_rejects_negative() -> None:
    assert _signal_input(position_version=0).position_version == 0
    with pytest.raises(ValidationError):
        _signal_input(position_version=-1)


def test_signal_input_requires_complete_target_values() -> None:
    values = _signal_input().model_dump()
    values.pop("targets")
    with pytest.raises(ValidationError):
        SignalInput.model_validate(values)


@pytest.mark.parametrize("view", ["state", "evaluation", "event"])
def test_signal_views_reject_naive_business_times(view: str) -> None:
    naive = datetime(2026, 7, 17, 9, 30)
    with pytest.raises(ValidationError):
        if view == "state":
            SignalStateView(
                subscription_id=uuid4(),
                zone=SignalZone.NORMAL,
                version=1,
                last_price_at=naive,
            )
        elif view == "evaluation":
            SignalEvaluationView(
                id=uuid4(),
                subscription_id=uuid4(),
                reason=EvaluationReason.SCHEDULED_QUOTE,
                result=EvaluationResult.SKIPPED,
                before_zone=SignalZone.UNKNOWN,
                after_zone=SignalZone.UNKNOWN,
                price=None,
                price_at=naive,
                hysteresis_applied=False,
                used_stale_target=False,
                content_hash="a" * 64,
                created_at=datetime(2026, 7, 17, 9, 31, tzinfo=UTC),
            )
        else:
            SignalEventView(
                id=uuid4(),
                subscription_id=uuid4(),
                evaluation_id=uuid4(),
                before_zone=SignalZone.UNKNOWN,
                after_zone=SignalZone.LOW,
                reason=EvaluationReason.SCHEDULED_QUOTE,
                price="9",
                price_at=naive,
                targets=TargetValues(
                    low_strong="8",
                    low_watch="9",
                    high_watch="11",
                    high_strong="12",
                ),
                target_revision_id=uuid4(),
                target_version=1,
                target_date=date(2026, 7, 17),
                position_status="NOT_HOLDING",
                position_version=0,
                quote_cycle_id=None,
                quote_item_id=None,
                used_stale_target=False,
                state_version=1,
                notification_class=NotificationClass.LOW,
                notification_eligible=True,
                created_at=datetime(2026, 7, 17, 9, 31, tzinfo=UTC),
            )


def test_skipped_evaluation_view_allows_missing_market_and_target_inputs() -> None:
    view = SignalEvaluationView(
        id=uuid4(),
        subscription_id=uuid4(),
        reason=EvaluationReason.SCHEDULED_QUOTE,
        result=EvaluationResult.SKIPPED,
        before_zone=SignalZone.UNKNOWN,
        after_zone=SignalZone.UNKNOWN,
        price=None,
        price_at=None,
        hysteresis_applied=False,
        used_stale_target=False,
        skip_code="TARGET_MISSING",
        content_hash="a" * 64,
        created_at=datetime(2026, 7, 17, 9, 31, tzinfo=UTC),
    )
    assert view.target_revision_id is None
    assert view.target_date is None
    assert view.targets is None
    assert view.price_version is None


def test_signal_evaluation_and_event_views_reject_naive_created_at() -> None:
    evaluation = {
        "id": uuid4(),
        "subscription_id": uuid4(),
        "reason": EvaluationReason.SCHEDULED_QUOTE,
        "result": EvaluationResult.SKIPPED,
        "before_zone": SignalZone.UNKNOWN,
        "after_zone": SignalZone.UNKNOWN,
        "hysteresis_applied": False,
        "used_stale_target": False,
        "content_hash": "a" * 64,
        "created_at": datetime(2026, 7, 17, 9, 31),
    }
    with pytest.raises(ValidationError):
        SignalEvaluationView(**evaluation)

    signal_event = {
        "id": uuid4(),
        "subscription_id": uuid4(),
        "evaluation_id": uuid4(),
        "before_zone": SignalZone.UNKNOWN,
        "after_zone": SignalZone.LOW,
        "reason": EvaluationReason.SCHEDULED_QUOTE,
        "price": "9",
        "price_at": datetime(2026, 7, 17, 9, 30, tzinfo=UTC),
        "targets": TargetValues(
            low_strong="8", low_watch="9", high_watch="11", high_strong="12"
        ),
        "target_revision_id": uuid4(),
        "target_version": 1,
        "target_date": date(2026, 7, 17),
        "position_status": "NOT_HOLDING",
        "position_version": 0,
        "used_stale_target": False,
        "state_version": 1,
        "notification_class": NotificationClass.LOW,
        "notification_eligible": True,
        "created_at": datetime(2026, 7, 17, 9, 31),
    }
    with pytest.raises(ValidationError):
        SignalEventView(**signal_event)


def test_position_snapshot_port_is_keyed_by_security_id() -> None:
    parameters = tuple(
        signature(PositionSnapshotPort.get_position_snapshot).parameters
    )
    assert parameters == ("self", "security_id")
