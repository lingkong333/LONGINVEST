"""Add idempotent backtest controls and execution generations.

Revision ID: 20260722_0014
Revises: 20260722_0013
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from long_invest.platform.config.settings import get_settings

revision: str = "20260722_0014"
down_revision: str | None = "20260722_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _application_role() -> str:
    role = get_settings().database_app_role
    if ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{role}"'


def upgrade() -> None:
    op.add_column(
        "backtest_task",
        sa.Column(
            "execution_generation",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        "backtest_task",
        sa.Column("rerun_from_task_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "backtest_task",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "backtest_task",
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_backtest_task_generation_positive",
        "backtest_task",
        "execution_generation > 0",
    )
    op.create_foreign_key(
        "fk_backtest_task_rerun_from_task_id_backtest_task",
        "backtest_task",
        "backtest_task",
        ["rerun_from_task_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "backtest_item",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "backtest_item",
        sa.Column("started_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "backtest_item",
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_backtest_item_attempt_count_nonnegative",
        "backtest_item",
        "attempt_count >= 0",
    )

    op.create_table(
        "backtest_control_command",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("result_task_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('PAUSE','RESUME','CANCEL','RETRY_FAILED','RERUN')",
            name="ck_backtest_control_command_action_valid",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_backtest_control_command_request_digest_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["result_task_id"], ["backtest_task.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["backtest_task.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_backtest_control_command_idempotency_key",
        ),
    )
    op.create_index(
        "ix_backtest_control_command_task_created",
        "backtest_control_command",
        ["task_id", "created_at"],
    )
    role = _application_role()
    op.execute(
        f"GRANT SELECT, INSERT ON TABLE backtest_control_command TO {role}"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backtest_control_command_task_created",
        table_name="backtest_control_command",
    )
    op.drop_table("backtest_control_command")
    op.drop_constraint(
        "ck_backtest_item_attempt_count_nonnegative",
        "backtest_item",
        type_="check",
    )
    op.drop_column("backtest_item", "ended_at")
    op.drop_column("backtest_item", "started_at")
    op.drop_column("backtest_item", "attempt_count")
    op.drop_constraint(
        "fk_backtest_task_rerun_from_task_id_backtest_task",
        "backtest_task",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_backtest_task_generation_positive",
        "backtest_task",
        type_="check",
    )
    op.drop_column("backtest_task", "terminal_at")
    op.drop_column("backtest_task", "updated_at")
    op.drop_column("backtest_task", "rerun_from_task_id")
    op.drop_column("backtest_task", "execution_generation")
