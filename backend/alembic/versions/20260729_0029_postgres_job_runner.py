"""Add V4 PostgreSQL task leases and checkpoints."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260729_0029"
down_revision: str | None = "20260728_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TRANSITIONAL_STATUSES = (
    "'PENDING','PENDING_DISPATCH','QUEUED','RUNNING','WAITING_RETRY',"
    "'PAUSING','PAUSED','CANCEL_REQUESTED','SUCCEEDED','PARTIAL','FAILED',"
    "'TIMED_OUT','LOST','CANCELED','BLOCKED','REJECTED'"
)
_LEGACY_STATUSES = _TRANSITIONAL_STATUSES.replace("'PENDING',", "")


def upgrade() -> None:
    op.add_column(
        "job",
        sa.Column(
            "module_owner", sa.String(64), server_default="legacy", nullable=False
        ),
    )
    op.add_column(
        "job",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "job",
        sa.Column("max_attempts", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "job",
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.add_column("job", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column(
        "job", sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "job", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job",
        sa.Column(
            "checkpoint",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("job", sa.Column("last_error_code", sa.String(100), nullable=True))
    op.add_column("job", sa.Column("last_error_summary", sa.String(500), nullable=True))
    op.add_column(
        "job",
        sa.Column(
            "pause_requested", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "recoverable", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "job",
        sa.Column("recovery_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "job",
        sa.Column("max_recoveries", sa.Integer(), server_default="1", nullable=False),
    )

    op.execute("UPDATE job SET module_owner = queue, next_run_at = created_at")
    op.drop_constraint(op.f("ck_job_status_valid"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_status_valid"),
        "job",
        f"status IN ({_TRANSITIONAL_STATUSES})",
    )
    op.create_check_constraint(
        op.f("ck_job_attempt_count_nonnegative"), "job", "attempt_count >= 0"
    )
    op.create_check_constraint(
        op.f("ck_job_attempt_limit_valid"),
        "job",
        "max_attempts > 0 AND max_attempts >= attempt_count",
    )
    op.create_check_constraint(
        op.f("ck_job_recovery_limit_valid"),
        "job",
        "recovery_count >= 0 AND max_recoveries >= recovery_count",
    )
    op.create_index(
        "ix_job_v4_due",
        "job",
        ["priority", "next_run_at", "created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_job_v4_expired_lease",
        "job",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    op.execute("UPDATE job SET status = 'PENDING_DISPATCH' WHERE status = 'PENDING'")
    op.drop_index("ix_job_v4_expired_lease", table_name="job")
    op.drop_index("ix_job_v4_due", table_name="job")
    op.drop_constraint(op.f("ck_job_recovery_limit_valid"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_attempt_limit_valid"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_attempt_count_nonnegative"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_status_valid"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_status_valid"), "job", f"status IN ({_LEGACY_STATUSES})"
    )
    for column in (
        "max_recoveries",
        "recovery_count",
        "recoverable",
        "cancel_requested",
        "pause_requested",
        "last_error_summary",
        "last_error_code",
        "checkpoint",
        "heartbeat_at",
        "lease_expires_at",
        "lease_token",
        "lease_owner",
        "next_run_at",
        "max_attempts",
        "attempt_count",
        "module_owner",
    ):
        op.drop_column("job", column)
