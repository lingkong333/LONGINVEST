from sqlalchemy import Index, UniqueConstraint, event

from long_invest.modules.monitoring.models import (
    MonitorSubscription,
    MonitorSubscriptionRevision,
    ScheduleOccurrence,
)


def test_monitoring_table_names_and_immutable_revision_shape() -> None:
    assert MonitorSubscription.__tablename__ == "monitor_subscription"
    assert MonitorSubscriptionRevision.__tablename__ == "monitor_subscription_revision"
    assert ScheduleOccurrence.__tablename__ == "schedule_occurrence"
    assert MonitorSubscription.__table__.c.current_revision_id.nullable is True
    assert {
        key.target_fullname
        for key in MonitorSubscription.__table__.c.current_revision_id.foreign_keys
    } == {"monitor_subscription_revision.id"}
    assert "version" in MonitorSubscription.__table__.c
    assert "config_version" not in MonitorSubscription.__table__.c
    assert "updated_at" not in MonitorSubscriptionRevision.__table__.c
    assert (
        MonitorSubscriptionRevision.__table__.c.parameters.type.__class__.__name__
        == "JSONB"
    )
    assert (
        ScheduleOccurrence.__table__.c.subscription_snapshot.type.__class__.__name__
        == "JSONB"
    )
    for model in (MonitorSubscription, MonitorSubscriptionRevision, ScheduleOccurrence):
        assert not model.__mapper__.relationships
    assert event.contains(
        MonitorSubscriptionRevision,
        "before_update",
        MonitorSubscriptionRevision._reject_mutation,
    )


def test_subscription_has_one_unarchived_row_per_security_partial_index() -> None:
    index = next(
        item
        for item in MonitorSubscription.__table__.indexes
        if item.name == "uq_monitor_subscription_open_security"
    )
    assert isinstance(index, Index) and index.unique is True
    assert "archived_at IS NULL" in str(index.dialect_options["postgresql"]["where"])


def test_subscription_revision_and_occurrence_scope_are_unique() -> None:
    revision_uniques = {
        tuple(column.name for column in item.columns)
        for item in MonitorSubscriptionRevision.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }
    occurrence_uniques = {
        tuple(column.name for column in item.columns)
        for item in ScheduleOccurrence.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }
    assert ("subscription_id", "revision_no") in revision_uniques
    assert ("occurrence_type", "schedule_id", "scheduled_at") in occurrence_uniques
    assert ScheduleOccurrence.__table__.c.scheduled_at.type.timezone is True
    assert MonitorSubscriptionRevision.__table__.c.hysteresis_ratio.type.precision == 10
    assert MonitorSubscriptionRevision.__table__.c.hysteresis_ratio.type.scale == 6
    assert MonitorSubscriptionRevision.__table__.c.notification_mode.nullable is False
    assert "notification_policy_id" not in MonitorSubscriptionRevision.__table__.c
