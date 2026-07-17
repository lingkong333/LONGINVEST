"""Create monitoring foundation storage.

Revision ID: 20260716_0010
Revises: 20260716_0009
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from long_invest.platform.config.settings import get_settings

revision: str = "20260716_0010"
down_revision: str | None = "20260716_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
TABLES = (
    "watchlist",
    "watchlist_item",
    "monitor_schedule",
    "monitor_schedule_revision",
    "user_position",
    "user_position_history",
    "monitor_subscription",
    "monitor_subscription_revision",
    "schedule_occurrence",
)
IMMUTABLE_TABLES = (
    "monitor_schedule_revision",
    "user_position_history",
    "monitor_subscription_revision",
)


def _application_role() -> str:
    role = get_settings().database_app_role
    if ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{role}"'


def upgrade() -> None:
    op.create_table(
        "watchlist",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_watchlist_version_positive")),
        sa.CheckConstraint(
            "display_order >= 0", name=op.f("ck_watchlist_display_order_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["app_user.id"],
            name="fk_watchlist_owner_user_id_app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_watchlist"),
    )
    op.create_table(
        "watchlist_item",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("watchlist_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["watchlist_id"],
            ["watchlist.id"],
            name="fk_watchlist_item_watchlist_id_watchlist",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_watchlist_item_security_id_security",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_watchlist_item"),
        sa.UniqueConstraint(
            "watchlist_id", "security_id", name="uq_watchlist_item_member"
        ),
    )
    op.create_table(
        "monitor_schedule",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("current_revision_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_monitor_schedule_version_positive")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monitor_schedule"),
    )
    op.create_table(
        "monitor_schedule_revision",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("times", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "metadata_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision_no > 0",
            name=op.f("ck_monitor_schedule_revision_revision_positive"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_monitor_schedule_revision_content_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["monitor_schedule.id"],
            name="fk_monitor_schedule_revision_schedule_id_monitor_schedule",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monitor_schedule_revision"),
        sa.UniqueConstraint(
            "schedule_id", "revision_no", name="uq_monitor_schedule_revision_number"
        ),
        sa.UniqueConstraint(
            "schedule_id",
            "idempotency_key",
            name="uq_monitor_schedule_revision_idempotency",
        ),
    )
    op.create_foreign_key(
        "fk_monitor_schedule_current_revision",
        "monitor_schedule",
        "monitor_schedule_revision",
        ["current_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "user_position",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("latest_history_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('HOLDING','NOT_HOLDING')",
            name=op.f("ck_user_position_status_valid"),
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_user_position_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_user_position_security_id_security",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_position"),
        sa.UniqueConstraint("security_id", name="uq_user_position_security"),
    )
    op.create_table(
        "user_position_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("position_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("before_status", sa.String(length=16), nullable=True),
        sa.Column("after_status", sa.String(length=16), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=False),
        sa.Column("position_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "before_status IS NULL OR before_status IN ('HOLDING','NOT_HOLDING')",
            name=op.f("ck_user_position_history_before_status_valid"),
        ),
        sa.CheckConstraint(
            "after_status IN ('HOLDING','NOT_HOLDING')",
            name=op.f("ck_user_position_history_after_status_valid"),
        ),
        sa.CheckConstraint(
            "position_version > 0",
            name=op.f("ck_user_position_history_position_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["user_position.id"],
            name="fk_user_position_history_position_id_user_position",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_user_position_history_security_id_security",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_position_history"),
        sa.UniqueConstraint(
            "security_id",
            "position_version",
            name="uq_user_position_history_security_version",
        ),
        sa.UniqueConstraint(
            "security_id",
            "idempotency_key",
            name="uq_user_position_history_idempotency",
        ),
    )
    op.create_foreign_key(
        "fk_user_position_latest_history",
        "user_position",
        "user_position_history",
        ["latest_history_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "monitor_subscription",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_revision_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('CONFIGURING','ENABLED','PAUSED','ARCHIVED')",
            name=op.f("ck_monitor_subscription_status_valid"),
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_monitor_subscription_version_positive")
        ),
        sa.CheckConstraint(
            "(status = 'ARCHIVED') = (archived_at IS NOT NULL)",
            name=op.f("ck_monitor_subscription_archive_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_monitor_subscription_security_id_security",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monitor_subscription"),
    )
    op.create_index(
        "uq_monitor_subscription_open_security",
        "monitor_subscription",
        ["security_id"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "monitor_subscription_revision",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=True),
        sa.Column("schedule_revision_id", sa.UUID(), nullable=True),
        sa.Column("target_mode", sa.String(length=16), nullable=False),
        sa.Column("target_version_id", sa.UUID(), nullable=True),
        sa.Column("strategy_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("hysteresis_ratio", sa.Numeric(10, 6), nullable=False),
        sa.Column("hysteresis_min", sa.Numeric(18, 6), nullable=False),
        sa.Column("notification_mode", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision_no > 0",
            name=op.f("ck_monitor_subscription_revision_revision_positive"),
        ),
        sa.CheckConstraint(
            "target_mode IN ('MANUAL','STRATEGY')",
            name=op.f("ck_monitor_subscription_revision_target_mode_valid"),
        ),
        sa.CheckConstraint(
            "hysteresis_ratio >= 0 AND hysteresis_min >= 0",
            name=op.f("ck_monitor_subscription_revision_hysteresis_nonnegative"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_monitor_subscription_revision_content_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["monitor_subscription.id"],
            name="fk_monitor_sub_revision_subscription",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["monitor_schedule.id"],
            name="fk_monitor_subscription_revision_schedule_id_monitor_schedule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_revision_id"],
            ["monitor_schedule_revision.id"],
            name="fk_monitor_sub_revision_schedule_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monitor_subscription_revision"),
        sa.UniqueConstraint(
            "subscription_id",
            "revision_no",
            name="uq_monitor_subscription_revision_number",
        ),
        sa.UniqueConstraint(
            "subscription_id",
            "idempotency_key",
            name="uq_monitor_subscription_revision_idempotency",
        ),
    )
    op.create_foreign_key(
        "fk_monitor_subscription_current_revision",
        "monitor_subscription",
        "monitor_subscription_revision",
        ["current_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "schedule_occurrence",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("occurrence_type", sa.String(length=64), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("schedule_revision_id", sa.UUID(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "subscription_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','CLAIMED','DISPATCHED','MISSED','FAILED')",
            name=op.f("ck_schedule_occurrence_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["monitor_schedule.id"],
            name="fk_schedule_occurrence_schedule_id_monitor_schedule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_revision_id"],
            ["monitor_schedule_revision.id"],
            name="fk_schedule_occurrence_schedule_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name="fk_schedule_occurrence_job_id_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_occurrence"),
        sa.UniqueConstraint(
            "occurrence_type",
            "schedule_id",
            "scheduled_at",
            name="uq_schedule_occurrence_scope",
        ),
    )

    role = _application_role()
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {', '.join(TABLES)} TO {role}"
    )
    for table_name in IMMUTABLE_TABLES:
        op.execute(f"REVOKE UPDATE, DELETE ON TABLE {table_name} FROM {role}")


def downgrade() -> None:
    op.drop_constraint(
        "fk_monitor_subscription_current_revision",
        "monitor_subscription",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_user_position_latest_history",
        "user_position",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_monitor_schedule_current_revision",
        "monitor_schedule",
        type_="foreignkey",
    )
    op.drop_table("schedule_occurrence")
    op.drop_table("monitor_subscription_revision")
    op.drop_table("monitor_subscription")
    op.drop_table("user_position_history")
    op.drop_table("user_position")
    op.drop_table("monitor_schedule_revision")
    op.drop_table("monitor_schedule")
    op.drop_table("watchlist_item")
    op.drop_table("watchlist")
