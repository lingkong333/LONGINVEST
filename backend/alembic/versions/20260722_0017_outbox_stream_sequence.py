"""add stable outbox stream sequence

Revision ID: 20260722_0017
Revises: 20260722_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260722_0017"
down_revision: str | Sequence[str] | None = "20260722_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_outbox",
        sa.Column(
            "stream_sequence",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_event_outbox_stream_sequence",
        "event_outbox",
        ["stream_sequence"],
    )
    op.create_index(
        "ix_event_outbox_topic_sequence",
        "event_outbox",
        ["topic", "stream_sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_event_outbox_topic_sequence", table_name="event_outbox")
    op.drop_constraint(
        "uq_event_outbox_stream_sequence",
        "event_outbox",
        type_="unique",
    )
    op.drop_column("event_outbox", "stream_sequence")
