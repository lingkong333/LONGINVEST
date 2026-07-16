from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2] / "alembic" / "versions" / "20260716_0009_qfq_refresh.py"
)


def test_qfq_migration_extends_the_single_main_chain() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260716_0009"' in source
    assert 'down_revision: str | None = "20260715_0008"' in source
    for table_name in ("qfq_dataset", "qfq_dataset_bar", "qfq_refresh_run"):
        assert f'"{table_name}"' in source
    assert "uq_qfq_dataset_current_security" in source
    assert "postgresql_where=sa.text(\"lifecycle = 'CURRENT'\")" in source


def test_qfq_migration_downgrades_in_dependency_order() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade() -> None:", maxsplit=1)[1]

    run = downgrade.index('op.drop_table("qfq_refresh_run")')
    bars = downgrade.index('op.drop_table("qfq_dataset_bar")')
    dataset = downgrade.index('op.drop_table("qfq_dataset")')
    assert run < bars < dataset


def test_qfq_migration_marks_convention_expanded_check_names_as_final() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    check_names = (
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
        "ck_qfq_dataset_bar_prices_positive",
        "ck_qfq_dataset_bar_ohlc_valid",
        "ck_qfq_dataset_bar_quantities_nonnegative",
        "ck_qfq_refresh_run_window_valid",
        "ck_qfq_refresh_run_input_daily_version_positive",
        "ck_qfq_refresh_run_expected_dates_nonempty",
        "ck_qfq_refresh_run_status_valid",
        "ck_qfq_refresh_run_result_consistent",
        "ck_qfq_refresh_run_completion_consistent",
        "ck_qfq_refresh_run_row_count_positive",
        "ck_qfq_refresh_run_checksum_sha256",
    )

    for check_name in check_names:
        assert f'op.f("{check_name}")' in source

    assert "uq_qfq_refresh_run_security_request_hash" in source
