"""Persist login rate limits in PostgreSQL.

Revision ID: 20260731_0036
Revises: 20260730_0035
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260731_0036"
down_revision: str | None = "20260730_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_login_rate_limit_attempt",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reservation_id", sa.String(length=64), nullable=False),
        sa.Column("ip_digest", sa.String(length=64), nullable=False),
        sa.Column("username_digest", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('PENDING','FAILED','SUCCEEDED')",
            name=op.f("ck_auth_login_rate_limit_attempt_outcome_valid"),
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_auth_login_rate_limit_attempt")
        ),
        sa.UniqueConstraint(
            "reservation_id",
            name=op.f("uq_auth_login_rate_limit_attempt_reservation_id"),
        ),
    )
    op.create_index(
        "ix_auth_login_rate_ip_window",
        "auth_login_rate_limit_attempt",
        ["ip_digest", "occurred_at"],
    )
    op.create_index(
        "ix_auth_login_rate_username_window",
        "auth_login_rate_limit_attempt",
        ["username_digest", "occurred_at"],
    )
    op.create_index(
        "ix_auth_login_rate_outcome_window",
        "auth_login_rate_limit_attempt",
        ["outcome", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_login_rate_outcome_window",
        table_name="auth_login_rate_limit_attempt",
    )
    op.drop_index(
        "ix_auth_login_rate_username_window",
        table_name="auth_login_rate_limit_attempt",
    )
    op.drop_index(
        "ix_auth_login_rate_ip_window",
        table_name="auth_login_rate_limit_attempt",
    )
    op.drop_table("auth_login_rate_limit_attempt")
