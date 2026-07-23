"""Persist complete strategy draft facts.

Revision ID: 20260723_0022
Revises: 20260722_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260723_0022"
down_revision: str | None = "20260722_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    for table_name in ("strategy_draft", "strategy_draft_revision"):
        op.add_column(
            table_name,
            sa.Column(
                "metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=EMPTY_JSON,
            ),
        )
        op.add_column(
            table_name,
            sa.Column(
                "parameter_schema",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=EMPTY_JSON,
            ),
        )


def downgrade() -> None:
    for table_name in ("strategy_draft_revision", "strategy_draft"):
        op.drop_column(table_name, "parameter_schema")
        op.drop_column(table_name, "metadata")
