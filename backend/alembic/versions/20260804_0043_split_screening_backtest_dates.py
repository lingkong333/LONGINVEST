"""split screening training dates from backtest dates

Revision ID: 20260804_0043
Revises: 20260804_0042
"""

from alembic import op

revision = "20260804_0043"
down_revision = "20260804_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_strategy_screening_period_date_range_valid"),
        "strategy_screening_period",
        type_="check",
    )
    op.alter_column(
        "strategy_screening_period", "test_start_date", nullable=True
    )
    op.alter_column(
        "strategy_screening_period", "test_end_date", nullable=True
    )
    op.create_check_constraint(
        op.f("ck_strategy_screening_period_training_range_valid"),
        "strategy_screening_period",
        "training_start_date <= training_end_date",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE strategy_screening_period "
        "SET test_start_date = training_end_date + 1, "
        "test_end_date = training_end_date + 1 "
        "WHERE test_start_date IS NULL OR test_end_date IS NULL"
    )
    op.drop_constraint(
        op.f("ck_strategy_screening_period_training_range_valid"),
        "strategy_screening_period",
        type_="check",
    )
    op.alter_column(
        "strategy_screening_period", "test_end_date", nullable=False
    )
    op.alter_column(
        "strategy_screening_period", "test_start_date", nullable=False
    )
    op.create_check_constraint(
        op.f("ck_strategy_screening_period_date_range_valid"),
        "strategy_screening_period",
        "training_start_date <= training_end_date "
        "AND training_end_date < test_start_date "
        "AND test_start_date <= test_end_date",
    )
