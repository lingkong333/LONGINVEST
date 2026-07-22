"""Add subscription notification policies, templates, and channel circuits.

Revision ID: 20260722_0019
Revises: 20260722_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260722_0019"
down_revision: str | None = "20260722_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitor_subscription_revision",
        sa.Column(
            "notification_channels",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE monitor_subscription_revision "
        "SET notification_mode = 'INHERIT', notification_channels = '[]'::jsonb "
        "WHERE notification_mode NOT IN ('INHERIT', 'CUSTOM')"
    )
    op.alter_column(
        "monitor_subscription_revision",
        "notification_mode",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_monitor_subscription_revision_notification_mode_valid"),
        "monitor_subscription_revision",
        "notification_mode IN ('INHERIT','CUSTOM')",
    )
    op.create_check_constraint(
        op.f("ck_monitor_subscription_revision_notification_channels_valid"),
        "monitor_subscription_revision",
        "notification_channels IN "
        "('[]'::jsonb, '[\"WECOM\"]'::jsonb, '[\"EMAIL\"]'::jsonb, "
        '\'["WECOM", "EMAIL"]\'::jsonb)',
    )
    op.create_check_constraint(
        op.f("ck_monitor_subscription_revision_notification_inherit_channels_empty"),
        "monitor_subscription_revision",
        "notification_mode = 'CUSTOM' OR notification_channels = '[]'::jsonb",
    )

    op.create_table(
        "notification_template_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_type", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("html", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=20), server_default="GIT", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_notification_template_version_content_hash_sha256"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_template_version")),
        sa.UniqueConstraint(
            "template_type",
            "content_hash",
            name="uq_notification_template_type_content_hash",
        ),
        sa.UniqueConstraint(
            "template_type",
            "version",
            name="uq_notification_template_type_version",
        ),
    )
    op.create_table(
        "notification_template_activation",
        sa.Column("template_type", sa.String(length=100), nullable=False),
        sa.Column("active_version", sa.String(length=100), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["template_type", "active_version"],
            [
                "notification_template_version.template_type",
                "notification_template_version.version",
            ],
            name="fk_notification_template_activation_active_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "template_type", name=op.f("pk_notification_template_activation")
        ),
    )
    op.create_table(
        "notification_channel_circuit",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("instance", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("cooldown_level", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('WECOM','EMAIL')",
            name=op.f("ck_notification_channel_circuit_channel_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('CLOSED','OPEN','HALF_OPEN','DISABLED')",
            name=op.f("ck_notification_channel_circuit_state_valid"),
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name=op.f("ck_notification_channel_circuit_failures_nonnegative"),
        ),
        sa.CheckConstraint(
            "cooldown_level BETWEEN 0 AND 2",
            name=op.f("ck_notification_channel_circuit_cooldown_level_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_channel_circuit")),
        sa.UniqueConstraint(
            "channel",
            "instance",
            name=op.f("uq_notification_channel_circuit_channel"),
        ),
    )
    op.add_column(
        "notification_delivery",
        sa.Column("circuit_deferred_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_delivery", "circuit_deferred_until")
    op.drop_table("notification_channel_circuit")
    op.drop_table("notification_template_activation")
    op.drop_table("notification_template_version")
    op.drop_constraint(
        op.f("ck_monitor_subscription_revision_notification_inherit_channels_empty"),
        "monitor_subscription_revision",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_monitor_subscription_revision_notification_channels_valid"),
        "monitor_subscription_revision",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_monitor_subscription_revision_notification_mode_valid"),
        "monitor_subscription_revision",
        type_="check",
    )
    op.alter_column(
        "monitor_subscription_revision",
        "notification_mode",
        existing_type=sa.String(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.drop_column("monitor_subscription_revision", "notification_channels")
