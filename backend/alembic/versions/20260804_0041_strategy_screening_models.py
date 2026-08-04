"""Add frozen full-market strategy screening models.

Revision ID: 20260804_0041
Revises: 20260803_0040
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260804_0041"
down_revision: str | None = "20260803_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_screening_batch",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("strategy_version_id", sa.UUID(), nullable=False),
        sa.Column("security_universe_snapshot_id", sa.UUID(), nullable=False),
        sa.Column(
            "parameter_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("parameter_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','PAUSING','PAUSED','SUCCEEDED',"
            "'PARTIAL','FAILED','CANCELING','CANCELED')",
            name=op.f("ck_strategy_screening_batch_status_valid"),
        ),
        sa.CheckConstraint(
            "parameter_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_strategy_screening_batch_parameter_hash_sha256"),
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_strategy_screening_batch_request_hash_sha256"),
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED','PARTIAL','FAILED','CANCELED') "
            "AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELED') "
            "AND completed_at IS NULL)",
            name=op.f("ck_strategy_screening_batch_completion_consistent"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name=op.f("ck_strategy_screening_batch_completion_time_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_strategy_screening_batch_job_id_job"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_universe_snapshot_id"],
            ["security_universe_snapshot.id"],
            name=op.f(
                "fk_strategy_screening_batch_security_universe_snapshot_id_security_universe_snapshot"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name=op.f(
                "fk_strategy_screening_batch_strategy_version_id_strategy_version"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_strategy_screening_batch")
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_strategy_screening_batch_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "job_id", name=op.f("uq_strategy_screening_batch_job_id")
        ),
    )
    op.create_index(
        op.f("ix_strategy_screening_batch_status_created"),
        "strategy_screening_batch",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_strategy_screening_batch_strategy_version_created"),
        "strategy_screening_batch",
        ["strategy_version_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "strategy_screening_period",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("training_start_date", sa.Date(), nullable=False),
        sa.Column("training_end_date", sa.Date(), nullable=False),
        sa.Column("test_start_date", sa.Date(), nullable=False),
        sa.Column("test_end_date", sa.Date(), nullable=False),
        sa.CheckConstraint(
            "sequence_no > 0",
            name=op.f("ck_strategy_screening_period_sequence_positive"),
        ),
        sa.CheckConstraint(
            "training_start_date <= training_end_date "
            "AND training_end_date < test_start_date "
            "AND test_start_date <= test_end_date",
            name=op.f("ck_strategy_screening_period_date_range_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["strategy_screening_batch.id"],
            name=op.f(
                "fk_strategy_screening_period_batch_id_strategy_screening_batch"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_screening_period")),
        sa.UniqueConstraint(
            "batch_id",
            "id",
            name=op.f("uq_strategy_screening_period_batch_id"),
        ),
        sa.UniqueConstraint(
            "batch_id",
            "sequence_no",
            name=op.f("uq_strategy_screening_period_batch_sequence"),
        ),
    )

    op.create_table(
        "strategy_screening_scope_item",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("qfq_dataset_id", sa.UUID(), nullable=True),
        sa.Column("qfq_data_version", sa.Integer(), nullable=True),
        sa.Column("qfq_data_hash", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "symbol ~ '^[0-9]{6}\\.(SH|SZ|BJ)$'",
            name=op.f("ck_strategy_screening_scope_item_symbol_valid"),
        ),
        sa.CheckConstraint(
            "(qfq_dataset_id IS NULL AND qfq_data_version IS NULL "
            "AND qfq_data_hash IS NULL) OR "
            "(qfq_dataset_id IS NOT NULL AND qfq_data_version > 0 "
            "AND qfq_data_hash ~ '^[0-9a-f]{64}$')",
            name=op.f(
                "ck_strategy_screening_scope_item_qfq_snapshot_consistent"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["strategy_screening_batch.id"],
            name=op.f(
                "fk_strategy_screening_scope_item_batch_id_strategy_screening_batch"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["qfq_dataset_id"],
            ["qfq_dataset.id"],
            name=op.f(
                "fk_strategy_screening_scope_item_qfq_dataset_id_qfq_dataset"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name=op.f(
                "fk_strategy_screening_scope_item_security_id_security"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_strategy_screening_scope_item")
        ),
        sa.UniqueConstraint(
            "batch_id",
            "id",
            name=op.f("uq_strategy_screening_scope_batch_id"),
        ),
        sa.UniqueConstraint(
            "batch_id",
            "security_id",
            name=op.f("uq_strategy_screening_scope_batch_security"),
        ),
    )
    op.create_index(
        op.f("ix_strategy_screening_scope_symbol"),
        "strategy_screening_scope_item",
        ["batch_id", "symbol"],
        unique=False,
    )

    op.create_table(
        "strategy_screening_result",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("period_id", sa.UUID(), nullable=False),
        sa.Column("scope_item_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("low_strong", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("low_watch", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("high_watch", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("high_strong", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column(
            "diagnostics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("training_data_hash", sa.String(length=64), nullable=True),
        sa.Column("training_row_count", sa.Integer(), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','MATCHED','NOT_MATCHED',"
            "'FAILED','CANCELED')",
            name=op.f("ck_strategy_screening_result_status_valid"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_strategy_screening_result_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "training_row_count IS NULL OR training_row_count > 0",
            name=op.f(
                "ck_strategy_screening_result_training_row_count_positive"
            ),
        ),
        sa.CheckConstraint(
            "training_data_hash IS NULL "
            "OR training_data_hash ~ '^[0-9a-f]{64}$'",
            name=op.f(
                "ck_strategy_screening_result_training_data_hash_sha256"
            ),
        ),
        sa.CheckConstraint(
            "(status = 'MATCHED' AND low_strong IS NOT NULL "
            "AND low_watch IS NOT NULL AND high_watch IS NOT NULL "
            "AND high_strong IS NOT NULL AND low_strong > 0 "
            "AND low_strong < low_watch AND low_watch < high_watch "
            "AND high_watch < high_strong "
            "AND high_strong < 'Infinity'::numeric "
            "AND reason IS NULL AND failure_code IS NULL "
            "AND training_data_hash IS NOT NULL "
            "AND training_row_count IS NOT NULL) OR "
            "(status = 'NOT_MATCHED' AND low_strong IS NULL "
            "AND low_watch IS NULL AND high_watch IS NULL "
            "AND high_strong IS NULL AND length(btrim(reason)) > 0 "
            "AND failure_code IS NULL AND training_data_hash IS NOT NULL "
            "AND training_row_count IS NOT NULL) OR "
            "(status = 'FAILED' AND low_strong IS NULL "
            "AND low_watch IS NULL AND high_watch IS NULL "
            "AND high_strong IS NULL AND reason IS NULL "
            "AND length(btrim(failure_code)) > 0) OR "
            "(status IN ('PENDING','RUNNING','CANCELED') "
            "AND low_strong IS NULL AND low_watch IS NULL "
            "AND high_watch IS NULL AND high_strong IS NULL "
            "AND reason IS NULL AND failure_code IS NULL)",
            name=op.f("ck_strategy_screening_result_outcome_consistent"),
        ),
        sa.CheckConstraint(
            "(status IN ('MATCHED','NOT_MATCHED','FAILED','CANCELED') "
            "AND ended_at IS NOT NULL) OR "
            "(status IN ('PENDING','RUNNING') AND ended_at IS NULL)",
            name=op.f("ck_strategy_screening_result_completion_consistent"),
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR ended_at IS NULL OR ended_at >= started_at",
            name=op.f("ck_strategy_screening_result_completion_time_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "period_id"],
            [
                "strategy_screening_period.batch_id",
                "strategy_screening_period.id",
            ],
            name=op.f("fk_strategy_screening_result_batch_period"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "scope_item_id"],
            [
                "strategy_screening_scope_item.batch_id",
                "strategy_screening_scope_item.id",
            ],
            name=op.f("fk_strategy_screening_result_batch_scope"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_strategy_screening_result")
        ),
        sa.UniqueConstraint(
            "period_id",
            "scope_item_id",
            name=op.f("uq_strategy_screening_result_period_scope"),
        ),
    )
    op.create_index(
        op.f("ix_strategy_screening_result_batch_status"),
        "strategy_screening_result",
        ["batch_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_strategy_screening_result_scope_period"),
        "strategy_screening_result",
        ["scope_item_id", "period_id"],
        unique=False,
    )

    _create_screening_triggers()


def _create_screening_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION longinvest_screening_batch_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status IN ('SUCCEEDED','CANCELED') THEN
                RAISE EXCEPTION 'completed strategy screening batch is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_strategy_screening_batch_immutable
        BEFORE UPDATE OR DELETE ON strategy_screening_batch
        FOR EACH ROW EXECUTE FUNCTION longinvest_screening_batch_immutable()
        """
    )
    op.execute(
        """
        CREATE FUNCTION longinvest_screening_child_mutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            target_batch_id uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                target_batch_id := OLD.batch_id;
            ELSE
                target_batch_id := NEW.batch_id;
            END IF;
            IF EXISTS (
                SELECT 1 FROM strategy_screening_batch
                WHERE id = target_batch_id
                  AND status IN ('SUCCEEDED','CANCELED')
            ) THEN
                RAISE EXCEPTION 'completed strategy screening batch is immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in (
        "strategy_screening_period",
        "strategy_screening_scope_item",
        "strategy_screening_result",
    ):
        op.execute(
            f"""
            CREATE TRIGGER tr_{table}_batch_mutable
            BEFORE INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION longinvest_screening_child_mutable()
            """
        )
    op.execute(
        """
        CREATE FUNCTION longinvest_screening_period_ordered()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(NEW.batch_id::text, 0));
            IF EXISTS (
                SELECT 1 FROM strategy_screening_period p
                WHERE p.batch_id = NEW.batch_id
                  AND p.id <> NEW.id
                  AND (
                    (p.sequence_no < NEW.sequence_no AND (
                        p.training_start_date > NEW.training_start_date OR
                        p.training_end_date > NEW.training_end_date OR
                        p.test_start_date > NEW.test_start_date OR
                        p.test_end_date > NEW.test_end_date
                    )) OR
                    (p.sequence_no > NEW.sequence_no AND (
                        p.training_start_date < NEW.training_start_date OR
                        p.training_end_date < NEW.training_end_date OR
                        p.test_start_date < NEW.test_start_date OR
                        p.test_end_date < NEW.test_end_date
                    ))
                  )
            ) THEN
                RAISE EXCEPTION 'screening period boundaries must not move backward'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_strategy_screening_period_ordered
        BEFORE INSERT OR UPDATE ON strategy_screening_period
        FOR EACH ROW EXECUTE FUNCTION longinvest_screening_period_ordered()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER tr_strategy_screening_period_ordered "
        "ON strategy_screening_period"
    )
    op.execute("DROP FUNCTION longinvest_screening_period_ordered()")
    for table in (
        "strategy_screening_result",
        "strategy_screening_scope_item",
        "strategy_screening_period",
    ):
        op.execute(f"DROP TRIGGER tr_{table}_batch_mutable ON {table}")
    op.execute("DROP FUNCTION longinvest_screening_child_mutable()")
    op.execute(
        "DROP TRIGGER tr_strategy_screening_batch_immutable "
        "ON strategy_screening_batch"
    )
    op.execute("DROP FUNCTION longinvest_screening_batch_immutable()")
    op.drop_index(
        op.f("ix_strategy_screening_result_scope_period"),
        table_name="strategy_screening_result",
    )
    op.drop_index(
        op.f("ix_strategy_screening_result_batch_status"),
        table_name="strategy_screening_result",
    )
    op.drop_table("strategy_screening_result")
    op.drop_index(
        op.f("ix_strategy_screening_scope_symbol"),
        table_name="strategy_screening_scope_item",
    )
    op.drop_table("strategy_screening_scope_item")
    op.drop_table("strategy_screening_period")
    op.drop_index(
        op.f("ix_strategy_screening_batch_strategy_version_created"),
        table_name="strategy_screening_batch",
    )
    op.drop_index(
        op.f("ix_strategy_screening_batch_status_created"),
        table_name="strategy_screening_batch",
    )
    op.drop_table("strategy_screening_batch")
