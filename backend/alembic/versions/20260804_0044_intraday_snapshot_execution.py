"""record intraday snapshot execution summaries

Revision ID: 20260804_0044
Revises: 20260804_0043
"""

import sqlalchemy as sa
from alembic import op

revision = "20260804_0044"
down_revision = "20260804_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_schedule_occurrence_status_valid"),
        "schedule_occurrence",
        type_="check",
    )
    op.add_column(
        "schedule_occurrence",
        sa.Column("trigger_type", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "schedule_occurrence",
        sa.Column("expected_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "schedule_occurrence",
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "schedule_occurrence",
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "schedule_occurrence",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "schedule_occurrence",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE schedule_occurrence SET trigger_type = 'AUTOMATIC' "
        "WHERE trigger_type IS NULL"
    )
    op.alter_column("schedule_occurrence", "trigger_type", nullable=False)
    op.create_check_constraint(
        op.f("ck_schedule_occurrence_status_valid"),
        "schedule_occurrence",
        "status IN ('PENDING','CLAIMED','DISPATCHED','RUNNING','SUCCEEDED',"
        "'PARTIAL','MISSED','FAILED')",
    )
    op.create_check_constraint(
        op.f("ck_schedule_occurrence_trigger_type_valid"),
        "schedule_occurrence",
        "trigger_type IN ('AUTOMATIC','MANUAL')",
    )
    op.create_check_constraint(
        op.f("ck_schedule_occurrence_counts_valid"),
        "schedule_occurrence",
        "expected_count >= 0 AND fetched_count >= 0 AND failed_count >= 0 "
        "AND fetched_count + failed_count <= expected_count",
    )
    op.create_check_constraint(
        op.f("ck_schedule_occurrence_execution_times_valid"),
        "schedule_occurrence",
        "completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)",
    )
    op.create_index(
        "uq_schedule_occurrence_manual_key",
        "schedule_occurrence",
        ["definition_key"],
        unique=True,
        postgresql_where=sa.text(
            "occurrence_type = 'REALTIME_QUOTE' AND trigger_type = 'MANUAL'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_schedule_occurrence_manual_key", table_name="schedule_occurrence"
    )
    op.drop_constraint(
        op.f("ck_schedule_occurrence_execution_times_valid"),
        "schedule_occurrence",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_schedule_occurrence_counts_valid"),
        "schedule_occurrence",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_schedule_occurrence_trigger_type_valid"),
        "schedule_occurrence",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_schedule_occurrence_status_valid"),
        "schedule_occurrence",
        type_="check",
    )
    for column in (
        "completed_at",
        "started_at",
        "failed_count",
        "fetched_count",
        "expected_count",
        "trigger_type",
    ):
        op.drop_column("schedule_occurrence", column)
    op.create_check_constraint(
        op.f("ck_schedule_occurrence_status_valid"),
        "schedule_occurrence",
        "status IN ('PENDING','CLAIMED','DISPATCHED','MISSED','FAILED')",
    )
