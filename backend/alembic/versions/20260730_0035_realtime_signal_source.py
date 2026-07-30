"""Store realtime source evidence directly on signal facts.

Revision ID: 20260730_0035
Revises: 20260730_0034
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260730_0035"
down_revision: str | None = "20260730_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, prefix in (
        ("signal_state", "last_quote"),
        ("signal_evaluation", "quote"),
        ("signal_event", "quote"),
    ):
        op.add_column(table, sa.Column(f"{prefix}_source", sa.String(32)))
        op.add_column(
            table,
            sa.Column(f"{prefix}_source_identity", postgresql.JSONB()),
        )


def downgrade() -> None:
    for table, prefix in reversed(
        (
            ("signal_state", "last_quote"),
            ("signal_evaluation", "quote"),
            ("signal_event", "quote"),
        )
    ):
        op.drop_column(table, f"{prefix}_source_identity")
        op.drop_column(table, f"{prefix}_source")
