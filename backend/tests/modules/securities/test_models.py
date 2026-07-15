from sqlalchemy import CheckConstraint, UniqueConstraint

from long_invest.modules.securities.models import (
    Security,
    SecurityMasterVersion,
    SecurityRevision,
    SecurityUniverseSnapshot,
    SecurityUniverseSnapshotItem,
)


def unique_columns(model: type) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.columns.keys())
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def check_sql(model: type) -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_security_owns_identity_provider_mapping_and_master_version() -> None:
    columns = set(Security.__table__.columns.keys())

    assert {
        "symbol",
        "exchange_code",
        "name",
        "market",
        "security_type",
        "listing_status",
        "listed_on",
        "delisted_on",
        "is_st",
        "is_suspended",
        "provider_codes",
        "master_version",
        "updated_at",
    } <= columns
    assert Security.__table__.c.symbol.unique is True
    checks = check_sql(Security)
    assert "master_version > 0" in checks
    assert "listing_status IN" in checks


def test_revision_is_append_only_fact_with_safe_before_and_after_snapshots() -> None:
    columns = set(SecurityRevision.__table__.columns.keys())

    assert {
        "security_id",
        "revision_no",
        "master_version",
        "changed_fields",
        "before_data",
        "after_data",
        "created_at",
    } <= columns
    assert ("security_id", "revision_no") in unique_columns(SecurityRevision)
    assert {fk.target_fullname for fk in SecurityRevision.__table__.foreign_keys} == {
        "security.id"
    }


def test_master_version_serializes_source_version_and_idempotency_claims() -> None:
    uniques = unique_columns(SecurityMasterVersion)

    assert ("source", "source_version") in uniques
    assert ("source", "idempotency_key") in uniques
    assert ("master_version",) in uniques


def test_universe_snapshot_freezes_filter_count_version_and_item_state() -> None:
    snapshot_columns = set(SecurityUniverseSnapshot.__table__.columns.keys())
    item_columns = set(SecurityUniverseSnapshotItem.__table__.columns.keys())

    assert {"filters", "item_count", "master_version", "created_at"} <= (
        snapshot_columns
    )
    assert {
        "snapshot_id",
        "symbol",
        "market",
        "security_type",
        "listing_status",
        "is_st",
        "is_suspended",
        "master_version",
    } <= item_columns
    assert ("snapshot_id", "symbol") in unique_columns(
        SecurityUniverseSnapshotItem
    )
