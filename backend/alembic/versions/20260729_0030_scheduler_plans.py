"""Expose the scheduler's loaded plans.

Revision ID: 20260729_0030
Revises: 20260729_0029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260729_0030"
down_revision: str | None = "20260729_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduler_runtime_state",
        sa.Column(
            "intraday_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "scheduler_runtime_state",
        sa.Column(
            "persistent_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column(
        "scheduler_runtime_state", "intraday_plan", server_default=None
    )
    op.alter_column(
        "scheduler_runtime_state", "persistent_plan", server_default=None
    )


def downgrade() -> None:
    op.drop_column("scheduler_runtime_state", "persistent_plan")
    op.drop_column("scheduler_runtime_state", "intraday_plan")
