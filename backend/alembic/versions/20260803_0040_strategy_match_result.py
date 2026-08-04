"""Add normal not-matched outcomes for strategy consumers.

Revision ID: 20260803_0040
Revises: 20260731_0039
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260803_0040"
down_revision: str | None = "20260731_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_target_calculation_run_status_valid"),
        "target_calculation_run",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_target_calculation_run_status_valid"),
        "target_calculation_run",
        "status IN ('PENDING','RUNNING','SUCCEEDED','NOT_MATCHED','FAILED')",
    )
    op.add_column(
        "backtest_item",
        sa.Column("outcome_reason", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.execute(
        "UPDATE target_calculation_run SET status = 'FAILED', "
        "failure_code = 'TARGET_CALCULATION_FAILED' "
        "WHERE status = 'NOT_MATCHED'"
    )
    op.drop_column("backtest_item", "outcome_reason")
    op.drop_constraint(
        op.f("ck_target_calculation_run_status_valid"),
        "target_calculation_run",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_target_calculation_run_status_valid"),
        "target_calculation_run",
        "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')",
    )
