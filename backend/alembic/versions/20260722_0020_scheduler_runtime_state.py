"""Add durable scheduler runtime heartbeat state.

Revision ID: 20260722_0020
Revises: 20260722_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260722_0020"
down_revision: str | None = "20260722_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_runtime_state",
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "clock_skew_seconds", sa.Float(), server_default="0", nullable=False
        ),
        sa.Column(
            "automatic_scheduling_paused",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("pause_reason", sa.String(length=300), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name=op.f("ck_scheduler_runtime_state_failures_non_negative"),
        ),
        sa.PrimaryKeyConstraint("role", name=op.f("pk_scheduler_runtime_state")),
    )


def downgrade() -> None:
    op.drop_table("scheduler_runtime_state")
