"""Add point-in-time corporate action evidence and backtest snapshots.

Revision ID: 20260722_0013
Revises: 20260721_0012
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from long_invest.platform.config.settings import get_settings

revision: str = "20260722_0013"
down_revision: str | None = "20260721_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "corporate_action_fetch_batch",
    "corporate_action_fact",
    "backtest_adjustment_snapshot",
)
ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _application_role() -> str:
    role = get_settings().database_app_role
    if ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{role}"'


def _protect_immutable_facts() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_corporate_action_fact_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'corporate action and backtest snapshots are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table_name in TABLES:
        op.execute(
            f"CREATE TRIGGER {table_name}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_corporate_action_fact_mutation()"
        )


def upgrade() -> None:
    op.create_table(
        "corporate_action_fetch_batch",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("provider_contract_version", sa.String(64), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=False),
        sa.Column("coverage_end", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "coverage_start <= coverage_end",
            name=op.f("ck_corporate_action_fetch_batch_coverage_window_valid"),
        ),
        sa.CheckConstraint(
            "observed_at <= fetched_at",
            name=op.f("ck_corporate_action_fetch_batch_timestamps_ordered"),
        ),
        sa.CheckConstraint(
            "row_count >= 0",
            name=op.f("ck_corporate_action_fetch_batch_row_count_non_negative"),
        ),
        sa.CheckConstraint(
            "status IN ('SUCCESS','FAILED')",
            name=op.f("ck_corporate_action_fetch_batch_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'SUCCESS' AND error_code IS NULL) OR "
            "(status = 'FAILED' AND error_code IS NOT NULL AND row_count = 0)",
            name=op.f("ck_corporate_action_fetch_batch_result_consistent"),
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_corporate_action_fetch_batch_content_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name=op.f("fk_corporate_action_fetch_batch_security_id_security"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corporate_action_fetch_batch")),
    )
    op.create_index(
        "ix_corporate_action_fetch_batch_coverage",
        "corporate_action_fetch_batch",
        ["security_id", "status", "observed_at", "coverage_start", "coverage_end"],
    )
    op.create_table(
        "corporate_action_fact",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("adjustment_factor", sa.Numeric(30, 18), nullable=False),
        sa.Column("source_reference", sa.String(500), nullable=False),
        sa.Column("raw_content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "event_date <= effective_date",
            name=op.f("ck_corporate_action_fact_event_dates_ordered"),
        ),
        sa.CheckConstraint(
            "published_at <= observed_at",
            name=op.f("ck_corporate_action_fact_publication_observed"),
        ),
        sa.CheckConstraint(
            "revision_no > 0",
            name=op.f("ck_corporate_action_fact_revision_positive"),
        ),
        sa.CheckConstraint(
            "adjustment_factor > 0 AND adjustment_factor <> 'NaN'::numeric "
            "AND adjustment_factor < 'Infinity'::numeric",
            name=op.f("ck_corporate_action_fact_factor_positive"),
        ),
        sa.CheckConstraint(
            "raw_content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_corporate_action_fact_raw_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["corporate_action_fetch_batch.id"],
            name=op.f("fk_corporate_action_fact_batch_id_corporate_action_fetch_batch"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name=op.f("fk_corporate_action_fact_security_id_security"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corporate_action_fact")),
        sa.UniqueConstraint(
            "security_id",
            "source",
            "source_event_id",
            "revision_no",
            name="uq_corporate_action_fact_source_event_revision",
        ),
        sa.UniqueConstraint(
            "security_id",
            "source",
            "source_event_id",
            "raw_content_hash",
            name="uq_corporate_action_fact_source_event_content",
        ),
    )
    op.create_index(
        "ix_corporate_action_fact_batch_effective",
        "corporate_action_fact",
        ["batch_id", "effective_date"],
    )
    op.create_index(
        "ix_corporate_action_fact_source_revision",
        "corporate_action_fact",
        ["security_id", "source", "source_event_id", "revision_no"],
    )
    op.create_table(
        "backtest_adjustment_snapshot",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("source_snapshot_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("provider_contract_version", sa.String(64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("entries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "frozen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "start_date <= end_date",
            name=op.f("ck_backtest_adjustment_snapshot_date_range_valid"),
        ),
        sa.CheckConstraint(
            "row_count >= 0",
            name=op.f("ck_backtest_adjustment_snapshot_row_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_backtest_adjustment_snapshot_content_hash_sha256"),
        ),
        sa.CheckConstraint(
            "fetched_at <= as_of",
            name=op.f("ck_backtest_adjustment_snapshot_fetch_before_knowledge_cutoff"),
        ),
        sa.CheckConstraint(
            "length(trim(source)) > 0 AND "
            "length(trim(provider_contract_version)) > 0",
            name=op.f("ck_backtest_adjustment_snapshot_required_text_nonblank"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entries) = 'array' "
            "AND jsonb_array_length(entries) = row_count",
            name=op.f("ck_backtest_adjustment_snapshot_entries_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["backtest_item.id"],
            name=op.f("fk_backtest_adjustment_snapshot_item_id_backtest_item"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name=op.f("fk_backtest_adjustment_snapshot_security_id_security"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_adjustment_snapshot")),
        sa.UniqueConstraint(
            "item_id", name="uq_backtest_adjustment_snapshot_item_id"
        ),
    )
    op.execute(
        """
        INSERT INTO provider_capability_setting
            (id, config_version, provider_code, capability, enabled, priority,
             concurrency, rate_per_second, timeout_seconds, auto_switch)
        VALUES
            ('20000000-0000-0000-0000-000000000007', 1, 'eastmoney',
             'CORPORATE_ACTIONS', true, 0, 2, 1.0, 20.0, false)
        """
    )
    _protect_immutable_facts()
    role = _application_role()
    op.execute(f"GRANT SELECT, INSERT ON TABLE {', '.join(TABLES)} TO {role}")
    op.execute(f"REVOKE UPDATE, DELETE ON TABLE {', '.join(TABLES)} FROM {role}")


def downgrade() -> None:
    for table_name in reversed(TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table_name}_append_only ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS reject_corporate_action_fact_mutation")
    op.execute(
        "ALTER TABLE provider_capability_setting "
        "DISABLE TRIGGER provider_capability_setting_append_only"
    )
    op.execute(
        "DELETE FROM provider_capability_setting "
        "WHERE id = '20000000-0000-0000-0000-000000000007'"
    )
    op.execute(
        "ALTER TABLE provider_capability_setting "
        "ENABLE TRIGGER provider_capability_setting_append_only"
    )
    op.drop_table("backtest_adjustment_snapshot")
    op.drop_index(
        "ix_corporate_action_fact_source_revision", table_name="corporate_action_fact"
    )
    op.drop_index(
        "ix_corporate_action_fact_batch_effective", table_name="corporate_action_fact"
    )
    op.drop_table("corporate_action_fact")
    op.drop_index(
        "ix_corporate_action_fetch_batch_coverage",
        table_name="corporate_action_fetch_batch",
    )
    op.drop_table("corporate_action_fetch_batch")
