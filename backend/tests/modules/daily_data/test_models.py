from sqlalchemy import ForeignKeyConstraint, PrimaryKeyConstraint, UniqueConstraint

from long_invest.modules.daily_data.models import (
    DailyBarRevision,
    DailyBarStage,
    DailyBarUnadjusted,
    DailyDataBatch,
)


def _constraint_names(table, kind) -> set[str | None]:
    return {item.name for item in table.constraints if isinstance(item, kind)}


def test_batch_and_stage_have_explicit_scope_constraints() -> None:
    assert "uq_daily_batch_scope" in _constraint_names(
        DailyDataBatch.__table__, UniqueConstraint
    )
    assert "uq_daily_stage_symbol" in _constraint_names(
        DailyBarStage.__table__, UniqueConstraint
    )


def test_unadjusted_bar_has_composite_primary_key_and_range_partition() -> None:
    table = DailyBarUnadjusted.__table__
    assert "pk_daily_bar_unadjusted" in _constraint_names(table, PrimaryKeyConstraint)
    assert [column.name for column in table.primary_key.columns] == [
        "security_id",
        "trade_date",
    ]
    assert table.dialect_options["postgresql"]["partition_by"] == ("RANGE (trade_date)")


def test_revision_has_composite_foreign_key_and_revision_uniqueness() -> None:
    table = DailyBarRevision.__table__
    assert "fk_daily_revision_bar" in _constraint_names(table, ForeignKeyConstraint)
    assert "uq_daily_bar_revision_no" in _constraint_names(table, UniqueConstraint)


def test_stage_has_seven_day_retention_deadline_and_named_indexes() -> None:
    assert "expires_at" in DailyBarStage.__table__.c
    assert all(index.name for index in DailyBarStage.__table__.indexes)
