import importlib
import importlib.util

from sqlalchemy import UniqueConstraint


def load_models():
    module_name = "long_invest.modules.notifications.models"
    assert importlib.util.find_spec(module_name) is not None, (
        "notification persistence models are not implemented"
    )
    return importlib.import_module(module_name)


def test_notification_records_keep_business_channel_and_attempt_facts_separate() -> (
    None
):
    models = load_models()

    event_columns = set(models.NotificationEvent.__table__.columns.keys())
    delivery_columns = set(models.NotificationDelivery.__table__.columns.keys())
    attempt_columns = set(models.NotificationDeliveryAttempt.__table__.columns.keys())

    assert {
        "event_type",
        "template_variables",
        "eligibility_status",
        "effective_channels",
    } <= event_columns
    assert {"channel", "config_version", "target_fingerprint"} <= delivery_columns
    assert {"attempt_no", "phase", "outcome", "possibly_delivered"} <= attempt_columns

    assert "attempt_no" not in event_columns | delivery_columns
    assert "channel" not in event_columns | attempt_columns
    assert "template_variables" not in delivery_columns | attempt_columns

    delivery_fks = {
        foreign_key.target_fullname
        for foreign_key in models.NotificationDelivery.__table__.foreign_keys
    }
    attempt_fks = {
        foreign_key.target_fullname
        for foreign_key in models.NotificationDeliveryAttempt.__table__.foreign_keys
    }
    assert delivery_fks == {"notification_event.id"}
    assert attempt_fks == {"notification_delivery.id"}


def test_notification_models_define_independent_idempotency_boundaries() -> None:
    models = load_models()

    assert models.NotificationEvent.__table__.c.idempotency_key.unique is True

    delivery_uniques = {
        tuple(constraint.columns.keys())
        for constraint in models.NotificationDelivery.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    attempt_uniques = {
        tuple(constraint.columns.keys())
        for constraint in models.NotificationDeliveryAttempt.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("event_id", "channel", "generation") in delivery_uniques
    assert ("delivery_id", "attempt_no") in attempt_uniques


def test_notification_statuses_match_v31_contract() -> None:
    models = load_models()

    assert {status.value for status in models.NotificationEventStatus} == {
        "ELIGIBLE",
        "SUPPRESSED",
        "DISPATCHED",
        "PARTIAL",
        "DELIVERED",
        "FAILED",
        "CANCELED",
    }
    assert {status.value for status in models.NotificationDeliveryStatus} == {
        "PENDING",
        "SENDING",
        "SENT",
        "RETRY_WAIT",
        "OUTCOME_UNKNOWN",
        "FAILED",
        "CANCELED",
        "SKIPPED_DISABLED",
        "SKIPPED_INELIGIBLE",
    }
