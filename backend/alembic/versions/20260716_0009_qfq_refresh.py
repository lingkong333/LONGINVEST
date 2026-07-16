"""Create QFQ refresh storage.

Revision ID: 20260716_0009
Revises: 20260715_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260716_0009"
down_revision: str | None = "20260715_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qfq_dataset",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("requested_start", sa.Date(), nullable=False),
        sa.Column("requested_end", sa.Date(), nullable=False),
        sa.Column("actual_start", sa.Date(), nullable=False),
        sa.Column("actual_end", sa.Date(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_contract_version", sa.String(length=64), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("anchor_close", sa.Numeric(18, 6), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("lifecycle", sa.String(length=16), nullable=False),
        sa.Column("freshness", sa.String(length=16), nullable=False),
        sa.Column("stale_reason", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name=op.f("ck_qfq_dataset_version_positive")),
        sa.CheckConstraint(
            "requested_start <= requested_end AND as_of_date = requested_end",
            name=op.f("ck_qfq_dataset_requested_window_valid"),
        ),
        sa.CheckConstraint(
            "actual_start <= actual_end "
            "AND actual_start >= requested_start "
            "AND actual_end <= requested_end",
            name=op.f("ck_qfq_dataset_actual_window_valid"),
        ),
        sa.CheckConstraint(
            "row_count > 0", name=op.f("ck_qfq_dataset_row_count_positive")
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name=op.f("ck_qfq_dataset_checksum_sha256"),
        ),
        sa.CheckConstraint(
            "anchor_close > 0 AND anchor_date = actual_end AND actual_end = as_of_date",
            name=op.f("ck_qfq_dataset_anchor_valid"),
        ),
        sa.CheckConstraint(
            "lifecycle IN ('STAGING','CURRENT','SUPERSEDED')",
            name=op.f("ck_qfq_dataset_lifecycle_valid"),
        ),
        sa.CheckConstraint(
            "(lifecycle = 'STAGING' AND activated_at IS NULL "
            "AND superseded_at IS NULL) "
            "OR (lifecycle = 'CURRENT' AND activated_at IS NOT NULL "
            "AND superseded_at IS NULL) "
            "OR (lifecycle = 'SUPERSEDED' AND activated_at IS NOT NULL "
            "AND superseded_at IS NOT NULL)",
            name=op.f("ck_qfq_dataset_lifecycle_timestamps_consistent"),
        ),
        sa.CheckConstraint(
            "freshness IN ('FRESH','STALE')",
            name=op.f("ck_qfq_dataset_freshness_valid"),
        ),
        sa.CheckConstraint(
            "(freshness = 'FRESH' AND stale_reason IS NULL) "
            "OR (freshness = 'STALE' AND stale_reason IS NOT NULL)",
            name=op.f("ck_qfq_dataset_freshness_reason_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_qfq_dataset_security_id_security",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_qfq_dataset"),
        sa.UniqueConstraint(
            "security_id", "version", name="uq_qfq_dataset_security_version"
        ),
    )
    op.create_index(
        "ix_qfq_dataset_security_lifecycle", "qfq_dataset", ["security_id", "lifecycle"]
    )
    op.create_index(
        "uq_qfq_dataset_current_security",
        "qfq_dataset",
        ["security_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle = 'CURRENT'"),
    )

    op.create_table(
        "qfq_dataset_bar",
        sa.Column("dataset_id", sa.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(18, 6), nullable=False),
        sa.Column("high", sa.Numeric(18, 6), nullable=False),
        sa.Column("low", sa.Numeric(18, 6), nullable=False),
        sa.Column("close", sa.Numeric(18, 6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(24, 4), nullable=False),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name=op.f("ck_qfq_dataset_bar_prices_positive"),
        ),
        sa.CheckConstraint(
            "high >= open AND high >= close AND high >= low "
            "AND low <= open AND low <= close AND low <= high",
            name=op.f("ck_qfq_dataset_bar_ohlc_valid"),
        ),
        sa.CheckConstraint(
            "volume >= 0 AND amount >= 0",
            name=op.f("ck_qfq_dataset_bar_quantities_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["qfq_dataset.id"],
            name="fk_qfq_dataset_bar_dataset_id_qfq_dataset",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("dataset_id", "trade_date", name="pk_qfq_dataset_bar"),
    )
    op.create_index("ix_qfq_dataset_bar_trade_date", "qfq_dataset_bar", ["trade_date"])

    op.create_table(
        "qfq_refresh_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("requested_start", sa.Date(), nullable=False),
        sa.Column("requested_end", sa.Date(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column(
            "expected_trade_dates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("input_daily_version", sa.Integer(), nullable=False),
        sa.Column("trigger_reason", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("candidate_dataset_id", sa.UUID(), nullable=True),
        sa.Column("activated_dataset_id", sa.UUID(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column(
            "result_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "requested_start <= requested_end AND as_of_date = requested_end",
            name=op.f("ck_qfq_refresh_run_window_valid"),
        ),
        sa.CheckConstraint(
            "input_daily_version > 0",
            name=op.f("ck_qfq_refresh_run_input_daily_version_positive"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_trade_dates) = 'array' "
            "AND jsonb_array_length(expected_trade_dates) > 0",
            name=op.f("ck_qfq_refresh_run_expected_dates_nonempty"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','FETCHING','VALIDATING','COMMITTING',"
            "'SUCCEEDED','FAILED','TIMED_OUT','SUPERSEDED')",
            name=op.f("ck_qfq_refresh_run_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'SUCCEEDED' AND activated_dataset_id IS NOT NULL "
            "AND candidate_dataset_id = activated_dataset_id "
            "AND row_count IS NOT NULL AND checksum IS NOT NULL "
            "AND error_code IS NULL) "
            "OR (status IN ('FAILED','TIMED_OUT','SUPERSEDED') "
            "AND candidate_dataset_id IS NULL AND activated_dataset_id IS NULL "
            "AND row_count IS NULL AND checksum IS NULL "
            "AND error_code IS NOT NULL) "
            "OR (status IN ('PENDING','FETCHING','VALIDATING','COMMITTING') "
            "AND candidate_dataset_id IS NULL AND activated_dataset_id IS NULL "
            "AND row_count IS NULL AND checksum IS NULL AND error_code IS NULL)",
            name=op.f("ck_qfq_refresh_run_result_consistent"),
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED','FAILED','TIMED_OUT','SUPERSEDED') "
            "AND completed_at IS NOT NULL) "
            "OR (status IN ('PENDING','FETCHING','VALIDATING','COMMITTING') "
            "AND completed_at IS NULL)",
            name=op.f("ck_qfq_refresh_run_completion_consistent"),
        ),
        sa.CheckConstraint(
            "row_count IS NULL OR row_count > 0",
            name=op.f("ck_qfq_refresh_run_row_count_positive"),
        ),
        sa.CheckConstraint(
            "checksum IS NULL OR length(checksum) = 64",
            name=op.f("ck_qfq_refresh_run_checksum_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["activated_dataset_id"],
            ["qfq_dataset.id"],
            name="fk_qfq_refresh_run_activated_dataset_id_qfq_dataset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_dataset_id"],
            ["qfq_dataset.id"],
            name="fk_qfq_refresh_run_candidate_dataset_id_qfq_dataset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name="fk_qfq_refresh_run_job_id_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_qfq_refresh_run_security_id_security",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_qfq_refresh_run"),
        sa.UniqueConstraint("job_id", name="uq_qfq_refresh_run_job_id"),
        sa.UniqueConstraint(
            "security_id", "idempotency_key", name="uq_qfq_refresh_run_idempotency"
        ),
        sa.UniqueConstraint(
            "security_id",
            "request_hash",
            name="uq_qfq_refresh_run_security_request_hash",
        ),
    )
    op.create_index(
        "ix_qfq_refresh_run_security_created",
        "qfq_refresh_run",
        ["security_id", "created_at"],
    )
    op.create_index(
        "ix_qfq_refresh_run_status_updated", "qfq_refresh_run", ["status", "updated_at"]
    )


def downgrade() -> None:
    op.drop_table("qfq_refresh_run")
    op.drop_table("qfq_dataset_bar")
    op.drop_table("qfq_dataset")
