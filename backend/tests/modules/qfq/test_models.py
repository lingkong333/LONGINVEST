from sqlalchemy import CheckConstraint, PrimaryKeyConstraint, UniqueConstraint

from long_invest.modules.qfq.models import QfqDataset, QfqDatasetBar, QfqRefreshRun


def _constraint_names(table, kind) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, kind) and constraint.name is not None
    }


def _check_sql(table, name: str) -> str:
    constraint = next(
        item
        for item in table.constraints
        if isinstance(item, CheckConstraint) and item.name == name
    )
    return str(constraint.sqltext)


def test_qfq_dataset_has_stable_constraints_and_one_current_index() -> None:
    table = QfqDataset.__table__

    assert table.name == "qfq_dataset"
    assert "uq_qfq_dataset_security_version" in _constraint_names(
        table, UniqueConstraint
    )
    assert {
        "ck_qfq_dataset_version_positive",
        "ck_qfq_dataset_requested_window_valid",
        "ck_qfq_dataset_actual_window_valid",
        "ck_qfq_dataset_row_count_positive",
        "ck_qfq_dataset_checksum_sha256",
        "ck_qfq_dataset_anchor_valid",
        "ck_qfq_dataset_lifecycle_valid",
        "ck_qfq_dataset_lifecycle_timestamps_consistent",
        "ck_qfq_dataset_freshness_valid",
        "ck_qfq_dataset_freshness_reason_consistent",
    }.issubset(_constraint_names(table, CheckConstraint))
    index = next(
        item for item in table.indexes if item.name == "uq_qfq_dataset_current_security"
    )
    assert index.unique is True
    assert "lifecycle = 'CURRENT'" in str(index.dialect_options["postgresql"]["where"])
    anchor_sql = _check_sql(table, "ck_qfq_dataset_anchor_valid")
    assert "anchor_close > 0" in anchor_sql
    assert "anchor_date = actual_end" in anchor_sql
    assert "actual_end = as_of_date" in anchor_sql


def test_qfq_dataset_bar_uses_dataset_and_date_primary_key() -> None:
    table = QfqDatasetBar.__table__

    assert table.name == "qfq_dataset_bar"
    primary = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, PrimaryKeyConstraint)
    )
    assert [column.name for column in primary.columns] == ["dataset_id", "trade_date"]
    assert table.c.open.type.precision == 18
    assert table.c.open.type.scale == 6
    assert table.c.amount.type.precision == 24
    assert table.c.amount.type.scale == 4
    assert {
        "ck_qfq_dataset_bar_prices_positive",
        "ck_qfq_dataset_bar_ohlc_valid",
        "ck_qfq_dataset_bar_quantities_nonnegative",
    }.issubset(_constraint_names(table, CheckConstraint))


def test_qfq_refresh_run_freezes_inputs_and_has_stable_status_constraints() -> None:
    table = QfqRefreshRun.__table__

    assert table.name == "qfq_refresh_run"
    assert table.c.job_id.unique is True
    assert table.c.expected_trade_dates.type.__class__.__name__ == "JSONB"
    assert table.c.input_daily_version.nullable is False
    assert table.c.request_hash.nullable is False
    assert table.c.candidate_dataset_id.nullable is True
    assert table.c.activated_dataset_id.nullable is True
    assert {
        "ck_qfq_refresh_run_window_valid",
        "ck_qfq_refresh_run_input_daily_version_positive",
        "ck_qfq_refresh_run_expected_dates_nonempty",
        "ck_qfq_refresh_run_status_valid",
        "ck_qfq_refresh_run_result_consistent",
        "ck_qfq_refresh_run_completion_consistent",
    }.issubset(_constraint_names(table, CheckConstraint))
    assert "uq_qfq_refresh_run_security_request_hash" in _constraint_names(
        table, UniqueConstraint
    )
    result_sql = _check_sql(table, "ck_qfq_refresh_run_result_consistent")
    assert "candidate_dataset_id IS NOT NULL" in result_sql
    assert "candidate_dataset_id = activated_dataset_id" in result_sql
    assert "row_count IS NOT NULL" in result_sql
    assert "checksum IS NOT NULL" in result_sql
    assert "activated_dataset_id IS NULL" in result_sql
