"""Persist provider request budgets and active leases.

Revision ID: 20260729_0031
Revises: 20260729_0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260729_0031"
down_revision: str | None = "20260729_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_provider_capability_setting_concurrency_range"),
        "provider_capability_setting",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_provider_capability_setting_concurrency_positive"),
        "provider_capability_setting",
        "concurrency >= 1",
    )
    op.add_column(
        "provider_capability_setting",
        sa.Column("daily_limit", sa.Integer(), server_default="50000", nullable=False),
    )
    op.add_column(
        "provider_capability_setting",
        sa.Column(
            "min_interval_seconds", sa.Float(), server_default="0.5", nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_provider_capability_setting_daily_limit_positive"),
        "provider_capability_setting",
        "daily_limit >= 1",
    )
    op.create_check_constraint(
        op.f("ck_provider_capability_setting_min_interval_nonnegative"),
        "provider_capability_setting",
        "min_interval_seconds >= 0",
    )
    op.alter_column("provider_capability_setting", "daily_limit", server_default=None)
    op.alter_column(
        "provider_capability_setting", "min_interval_seconds", server_default=None
    )

    op.create_table(
        "provider_budget_policy",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("provider_code", sa.String(length=32), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("reset_timezone", sa.String(length=64), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("realtime_reserved", sa.Integer(), nullable=False),
        sa.Column("daily_reserved", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "daily_limit >= 1",
            name=op.f("ck_provider_budget_policy_daily_limit_positive"),
        ),
        sa.CheckConstraint(
            "max_concurrency >= 1",
            name=op.f("ck_provider_budget_policy_max_concurrency_positive"),
        ),
        sa.CheckConstraint(
            "realtime_reserved >= 0",
            name=op.f("ck_provider_budget_policy_realtime_reserved_nonnegative"),
        ),
        sa.CheckConstraint(
            "daily_reserved >= 0",
            name=op.f("ck_provider_budget_policy_daily_reserved_nonnegative"),
        ),
        sa.CheckConstraint(
            "realtime_reserved + daily_reserved < daily_limit",
            name=op.f("ck_provider_budget_policy_reserved_below_limit"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_budget_policy")),
        sa.UniqueConstraint(
            "config_version",
            "provider_code",
            name=op.f("uq_provider_budget_policy_config_version"),
        ),
    )
    op.execute(
        """
        INSERT INTO provider_budget_policy
            (id, config_version, provider_code, daily_limit, reset_timezone,
             max_concurrency, realtime_reserved, daily_reserved)
        SELECT gen_random_uuid(), version, provider_code, 50000, 'Asia/Shanghai',
               8, 500, 500
        FROM provider_config_version
        """
    )
    op.create_table(
        "provider_budget_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_code", sa.String(length=32), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("budget_date", sa.Date(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("last_request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_limit_reason", sa.String(length=100), nullable=True),
        sa.Column("latest_limited_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "used_count >= 0",
            name=op.f("ck_provider_budget_usage_used_count_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_budget_usage")),
        sa.UniqueConstraint(
            "provider_code",
            "capability",
            "budget_date",
            name=op.f("uq_provider_budget_usage_provider_code"),
        ),
    )
    op.create_index(
        op.f("ix_provider_budget_usage_provider_date"),
        "provider_budget_usage",
        ["provider_code", "budget_date"],
    )
    op.create_table(
        "provider_request_lease",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("provider_code", sa.String(length=32), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_request_lease")),
        sa.UniqueConstraint("token", name=op.f("uq_provider_request_lease_token")),
    )
    op.create_index(
        op.f("ix_provider_request_lease_active"),
        "provider_request_lease",
        ["provider_code", "capability", "expires_at"],
        postgresql_where=sa.text("released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_provider_request_lease_active"),
        table_name="provider_request_lease",
    )
    op.drop_table("provider_request_lease")
    op.drop_index(
        op.f("ix_provider_budget_usage_provider_date"),
        table_name="provider_budget_usage",
    )
    op.drop_table("provider_budget_usage")
    op.drop_table("provider_budget_policy")
    op.drop_constraint(
        op.f("ck_provider_capability_setting_min_interval_nonnegative"),
        "provider_capability_setting",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_provider_capability_setting_daily_limit_positive"),
        "provider_capability_setting",
        type_="check",
    )
    op.drop_column("provider_capability_setting", "min_interval_seconds")
    op.drop_column("provider_capability_setting", "daily_limit")
    op.drop_constraint(
        op.f("ck_provider_capability_setting_concurrency_positive"),
        "provider_capability_setting",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_provider_capability_setting_concurrency_range"),
        "provider_capability_setting",
        "concurrency BETWEEN 1 AND 32",
    )
