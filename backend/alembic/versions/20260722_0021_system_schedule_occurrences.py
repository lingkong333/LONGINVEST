"""Support system-owned schedule occurrences.

Revision ID: 20260722_0021
Revises: 20260722_0020
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from long_invest.platform.config.settings import get_settings

revision: str = "20260722_0021"
down_revision: str | None = "20260722_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _application_role() -> str:
    role = get_settings().database_app_role
    if ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{role}"'


def upgrade() -> None:
    op.alter_column("schedule_occurrence", "schedule_id", nullable=True)
    op.alter_column("schedule_occurrence", "schedule_revision_id", nullable=True)
    op.add_column(
        "schedule_occurrence",
        sa.Column("definition_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "schedule_occurrence",
        sa.Column("scheduled_trade_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "schedule_occurrence",
        sa.Column("calendar_version_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_schedule_occurrence_calendar_version_id_trading_calendar_version"),
        "schedule_occurrence",
        "trading_calendar_version",
        ["calendar_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_schedule_occurrence_definition_scope_valid"),
        "schedule_occurrence",
        "(schedule_id IS NOT NULL AND schedule_revision_id IS NOT NULL "
        "AND definition_key IS NULL) OR "
        "(schedule_id IS NULL AND schedule_revision_id IS NULL "
        "AND definition_key IS NOT NULL)",
    )
    op.create_index(
        "uq_schedule_occurrence_system_scope",
        "schedule_occurrence",
        ["occurrence_type", "definition_key", "scheduled_at"],
        unique=True,
        postgresql_where=sa.text("definition_key IS NOT NULL"),
    )
    role = _application_role()
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"scheduler_runtime_state TO {role}"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM schedule_occurrence "
        "WHERE definition_key IS NOT NULL) THEN RAISE EXCEPTION "
        "'system schedule occurrences exist'; END IF; END $$"
    )
    op.drop_index(
        "uq_schedule_occurrence_system_scope", table_name="schedule_occurrence"
    )
    op.drop_constraint(
        op.f("ck_schedule_occurrence_definition_scope_valid"),
        "schedule_occurrence",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_schedule_occurrence_calendar_version_id_trading_calendar_version"),
        "schedule_occurrence",
        type_="foreignkey",
    )
    op.drop_column("schedule_occurrence", "calendar_version_id")
    op.drop_column("schedule_occurrence", "scheduled_trade_date")
    op.drop_column("schedule_occurrence", "definition_key")
    op.alter_column("schedule_occurrence", "schedule_revision_id", nullable=False)
    op.alter_column("schedule_occurrence", "schedule_id", nullable=False)
