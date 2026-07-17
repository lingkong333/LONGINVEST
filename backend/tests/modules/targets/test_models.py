from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from long_invest.modules.targets.models import (
    SubscriptionTargetBinding,
    TargetRevision,
)


def _constraint_names(model: type) -> set[str | None]:
    return {item.name for item in model.__table__.constraints}


def _unique_columns(model: type) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in item.columns)
        for item in model.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }


def test_target_tables_own_expected_columns() -> None:
    assert TargetRevision.__tablename__ == "target_revision"
    assert SubscriptionTargetBinding.__tablename__ == "subscription_target_binding"
    assert {
        "low_strong",
        "low_watch",
        "high_watch",
        "high_strong",
        "source",
        "source_revision_id",
        "created_at",
    } <= set(TargetRevision.__table__.c.keys())
    for name in ("low_strong", "low_watch", "high_watch", "high_strong"):
        column_type = TargetRevision.__table__.c[name].type
        assert column_type.precision == 20
        assert column_type.scale == 2
    assert not TargetRevision.__mapper__.relationships
    assert not SubscriptionTargetBinding.__mapper__.relationships


def test_target_revision_has_order_source_and_version_constraints() -> None:
    names = _constraint_names(TargetRevision)
    assert {
        "ck_target_revision_values_ordered",
        "ck_target_revision_source_valid",
        "ck_target_revision_revision_positive",
    } <= names
    assert ("subscription_id", "revision_no") in _unique_columns(TargetRevision)
    assert ("subscription_id", "idempotency_key") in _unique_columns(TargetRevision)


def test_target_binding_is_unique_and_constrained() -> None:
    assert ("subscription_id",) in _unique_columns(SubscriptionTargetBinding)
    names = _constraint_names(SubscriptionTargetBinding)
    assert "ck_subscription_target_binding_status_valid" in names
    assert "ck_subscription_target_binding_version_positive" in names
    assert any(
        isinstance(item, Index)
        and tuple(column.name for column in item.columns) == ("status",)
        for item in SubscriptionTargetBinding.__table__.indexes
    )
    assert any(
        isinstance(item, CheckConstraint)
        and "READY" in str(item.sqltext)
        and "MISSING" in str(item.sqltext)
        for item in SubscriptionTargetBinding.__table__.constraints
    )
