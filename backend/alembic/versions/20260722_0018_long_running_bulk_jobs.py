"""Allow bounded long-running bulk jobs.

Revision ID: 20260722_0018
Revises: 20260722_0017
"""

from alembic import op

revision = "20260722_0018"
down_revision = "20260722_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_job_hard_timeout_not_less_than_soft", "job", type_="check"
    )
    op.create_check_constraint(
        "ck_job_hard_timeout_not_less_than_soft",
        "job",
        "hard_timeout_seconds >= soft_timeout_seconds "
        "AND hard_timeout_seconds <= 86400",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_job_hard_timeout_not_less_than_soft", "job", type_="check"
    )
    op.create_check_constraint(
        "ck_job_hard_timeout_not_less_than_soft",
        "job",
        "hard_timeout_seconds >= soft_timeout_seconds "
        "AND hard_timeout_seconds <= 3600",
    )
