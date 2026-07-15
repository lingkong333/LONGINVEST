from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from long_invest.modules.market_data.models import DataQualityIssue


def test_data_quality_issue_has_required_persistence_fields() -> None:
    assert set(DataQualityIssue.__table__.columns.keys()) == {
        "id",
        "issue_type",
        "subject_type",
        "subject_id",
        "symbol",
        "status",
        "severity",
        "evidence",
        "dedupe_key",
        "occurrence_count",
        "first_seen_at",
        "last_seen_at",
        "resolved_at",
        "resolved_by_user_id",
        "resolution_action",
        "resolution_reason",
        "selected_source",
    }
    assert DataQualityIssue.__table__.c.symbol.nullable is True
    assert DataQualityIssue.__table__.c.evidence.nullable is False
    assert DataQualityIssue.__table__.c.occurrence_count.default.arg == 1


def test_data_quality_issue_constraints_have_stable_explicit_names() -> None:
    constraints = DataQualityIssue.__table__.constraints
    unique_names = {
        item.name for item in constraints if isinstance(item, UniqueConstraint)
    }
    check_names = {
        item.name for item in constraints if isinstance(item, CheckConstraint)
    }

    assert "uq_data_quality_issue_dedupe_key" in unique_names
    assert "ck_data_quality_issue_occurrence_count_positive" in check_names
    assert "ck_data_quality_issue_status_valid" in check_names
    assert "ck_data_quality_issue_severity_valid" in check_names
    assert "ck_data_quality_issue_evidence_non_empty_object" in check_names


def test_quality_issue_database_rejects_invalid_severity_and_evidence() -> None:
    sql = str(
        CreateTable(DataQualityIssue.__table__).compile(dialect=postgresql.dialect())
    )

    assert "severity IN ('INFO','WARNING','ERROR','CRITICAL')" in sql
    assert "jsonb_typeof(evidence) = 'object'" in sql
    assert "evidence <> '{}'::jsonb" in sql


def test_data_quality_issue_has_stable_query_indexes() -> None:
    indexes = {
        item.name: tuple(column.name for column in item.columns)
        for item in DataQualityIssue.__table__.indexes
        if isinstance(item, Index)
    }

    assert indexes == {
        "ix_data_quality_issue_status_last_seen": ("status", "last_seen_at"),
        "ix_data_quality_issue_symbol_status": ("symbol", "status"),
    }
