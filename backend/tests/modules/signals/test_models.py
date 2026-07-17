from sqlalchemy import Index, UniqueConstraint

from long_invest.modules.signals.models import (
    SignalEvaluation,
    SignalEvent,
    SignalState,
)


def _constraint_names(model: type) -> set[str | None]:
    return {item.name for item in model.__table__.constraints}


def _unique_columns(model: type) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in item.columns)
        for item in model.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }


def test_signal_tables_and_current_state_shape() -> None:
    assert SignalState.__tablename__ == "signal_state"
    assert SignalEvaluation.__tablename__ == "signal_evaluation"
    assert SignalEvent.__tablename__ == "signal_event"
    assert ("subscription_id",) in _unique_columns(SignalState)
    assert "ck_signal_state_zone_valid" in _constraint_names(SignalState)
    assert "ck_signal_state_version_positive" in _constraint_names(SignalState)
    assert SignalState.__table__.c.last_price_at.type.timezone is True


def test_signal_evaluation_preserves_inputs_and_guards_idempotency() -> None:
    columns = set(SignalEvaluation.__table__.c.keys())
    assert {
        "subscription_version",
        "target_revision_id",
        "target_version",
        "position_version",
        "price",
        "price_at",
        "hysteresis_applied",
        "used_stale_target",
        "skip_code",
    } <= columns
    assert ("subscription_id", "idempotency_key") in _unique_columns(
        SignalEvaluation
    )
    assert {
        "ck_signal_evaluation_reason_valid",
        "ck_signal_evaluation_result_valid",
        "ck_signal_evaluation_before_zone_valid",
        "ck_signal_evaluation_after_zone_valid",
        "ck_signal_evaluation_versions_positive",
    } <= _constraint_names(SignalEvaluation)


def test_signal_event_preserves_real_transition_and_notification_decision() -> None:
    columns = set(SignalEvent.__table__.c.keys())
    assert {
        "before_zone",
        "after_zone",
        "price",
        "price_at",
        "target_revision_id",
        "target_version",
        "position_version",
        "state_version",
        "notification_class",
        "notification_eligible",
        "suppression_reason",
    } <= columns
    assert {
        "ck_signal_event_before_zone_valid",
        "ck_signal_event_after_zone_valid",
        "ck_signal_event_real_transition",
        "ck_signal_event_notification_class_valid",
        "ck_signal_event_versions_positive",
    } <= _constraint_names(SignalEvent)
    indexed = {
        tuple(column.name for column in item.columns)
        for item in SignalEvent.__table__.indexes
        if isinstance(item, Index)
    }
    assert ("subscription_id", "created_at") in indexed
    assert not SignalState.__mapper__.relationships
    assert not SignalEvaluation.__mapper__.relationships
    assert not SignalEvent.__mapper__.relationships
