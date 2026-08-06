"""Add notification recipients and signal bindings.

Revision ID: 20260806_0048
Revises: 20260804_0047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260806_0048"
down_revision: str | None = "20260804_0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_recipient",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("recipient_type", sa.String(20), nullable=False),
        sa.Column("destination", sa.String(200), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("secret_fingerprint", sa.String(32), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
        sa.CheckConstraint(
            "recipient_type IN ('EMAIL','WECOM_ROBOT','WECOM_USER')",
            name=op.f("ck_notification_recipient_recipient_type_valid"),
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_notification_recipient_version_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_recipient")),
        sa.UniqueConstraint(
            "recipient_type",
            "name",
            name=op.f("uq_notification_recipient_recipient_type"),
        ),
    )
    op.create_index(
        "ix_notification_recipient_enabled",
        "notification_recipient",
        ["enabled", "recipient_type"],
    )
    op.create_table(
        "signal_notification_binding",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recipient_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_by_user_id", sa.String(64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_signal_notification_binding_version_positive")
        ),
        sa.PrimaryKeyConstraint(
            "subscription_id", name=op.f("pk_signal_notification_binding")
        ),
    )
    op.add_column(
        "notification_delivery",
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "notification_delivery",
        sa.Column("recipient_name", sa.String(100), nullable=True),
    )
    op.add_column(
        "notification_delivery",
        sa.Column("recipient_type", sa.String(20), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_notification_delivery_recipient_id_notification_recipient"),
        "notification_delivery",
        "notification_recipient",
        ["recipient_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_notification_delivery_event_id", "notification_delivery", type_="unique"
    )
    op.create_unique_constraint(
        "uq_notification_delivery_event_recipient_generation",
        "notification_delivery",
        ["event_id", "recipient_id", "channel", "generation"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_notification_delivery_event_recipient_generation",
        "notification_delivery",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_notification_delivery_event_id",
        "notification_delivery",
        ["event_id", "channel", "generation"],
    )
    op.drop_constraint(
        op.f("fk_notification_delivery_recipient_id_notification_recipient"),
        "notification_delivery",
        type_="foreignkey",
    )
    op.drop_column("notification_delivery", "recipient_type")
    op.drop_column("notification_delivery", "recipient_name")
    op.drop_column("notification_delivery", "recipient_id")
    op.drop_table("signal_notification_binding")
    op.drop_index(
        "ix_notification_recipient_enabled", table_name="notification_recipient"
    )
    op.drop_table("notification_recipient")
