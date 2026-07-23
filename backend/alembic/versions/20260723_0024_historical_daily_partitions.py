"""Add historical partitions for unadjusted daily bars.

Revision ID: 20260723_0024
Revises: 20260723_0023
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260723_0024"
down_revision: str | None = "20260723_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HISTORICAL_PARTITION_YEARS = tuple(range(1990, 2025))


def upgrade() -> None:
    for year in HISTORICAL_PARTITION_YEARS:
        op.execute(
            f"""
            CREATE TABLE daily_bar_unadjusted_{year}
            PARTITION OF daily_bar_unadjusted
            FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
            """
        )


def downgrade() -> None:
    for year in reversed(HISTORICAL_PARTITION_YEARS):
        op.execute(f"DROP TABLE daily_bar_unadjusted_{year}")
