"""Store full-market daily collection plans and progress.

Revision ID: 20260730_0034
Revises: 20260730_0033
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260730_0034"
down_revision: str | None = "20260730_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "daily_data_batch",
        sa.Column("requested_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "daily_data_batch",
        sa.Column(
            "pending_retry_count", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "daily_data_batch",
        sa.Column(
            "plan_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_daily_data_batch_daily_batch_progress_nonnegative"),
        "daily_data_batch",
        "requested_count >= 0 AND pending_retry_count >= 0",
    )
    for column in ("requested_count", "pending_retry_count", "plan_snapshot"):
        op.alter_column("daily_data_batch", column, server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_daily_data_batch_daily_batch_progress_nonnegative"),
        "daily_data_batch",
        type_="check",
    )
    op.drop_column("daily_data_batch", "plan_snapshot")
    op.drop_column("daily_data_batch", "pending_retry_count")
    op.drop_column("daily_data_batch", "requested_count")
