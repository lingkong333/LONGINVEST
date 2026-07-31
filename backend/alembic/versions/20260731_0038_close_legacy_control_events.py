"""Close control events left by already canceled legacy test jobs.

Revision ID: 20260731_0038
Revises: 20260731_0037
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260731_0038"
down_revision: str | None = "20260731_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE event_outbox AS event
        SET status = 'DEAD',
            last_error_code = 'LEGACY_RQ_REMOVED',
            last_error_summary =
                '旧版测试任务控制请求随 Redis/RQ 兼容层退役而关闭',
            locked_at = NULL,
            locked_by = NULL,
            updated_at = now()
        FROM job
        WHERE event.topic = 'jobs.control'
          AND event.status IN ('PENDING', 'DISPATCHING')
          AND event.aggregate_id = job.id::text
          AND job.job_type = 'ADMIN_TEST'
          AND job.queue = 'maintenance'
          AND job.status = 'CANCELED'
        """
    )


def downgrade() -> None:
    # Closed legacy control requests must never become executable again.
    pass
