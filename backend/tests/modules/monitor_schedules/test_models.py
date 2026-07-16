from sqlalchemy import UniqueConstraint, event

from long_invest.modules.monitor_schedules.models import (
    MonitorSchedule,
    MonitorScheduleRevision,
)


def test_schedule_models_have_current_pointer_and_immutable_revision_shape() -> None:
    assert MonitorSchedule.__tablename__ == "monitor_schedule"
    assert MonitorScheduleRevision.__tablename__ == "monitor_schedule_revision"
    assert MonitorSchedule.__table__.c.current_revision_id.nullable is True
    assert {
        key.target_fullname
        for key in MonitorSchedule.__table__.c.current_revision_id.foreign_keys
    } == {"monitor_schedule_revision.id"}
    assert MonitorScheduleRevision.__table__.c.times.type.__class__.__name__ == "JSONB"
    assert "updated_at" not in MonitorScheduleRevision.__table__.c
    assert not MonitorSchedule.__mapper__.relationships
    assert not MonitorScheduleRevision.__mapper__.relationships
    assert event.contains(
        MonitorScheduleRevision,
        "before_update",
        MonitorScheduleRevision._reject_mutation,
    )


def test_schedule_revision_number_is_unique_and_versions_positive() -> None:
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in MonitorScheduleRevision.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("schedule_id", "revision_no") in uniques
    assert "ck_monitor_schedule_version_positive" in {
        item.name for item in MonitorSchedule.__table__.constraints
    }
    assert "current_version" not in MonitorSchedule.__table__.c
    assert "ck_monitor_schedule_revision_revision_positive" in {
        item.name for item in MonitorScheduleRevision.__table__.constraints
    }
