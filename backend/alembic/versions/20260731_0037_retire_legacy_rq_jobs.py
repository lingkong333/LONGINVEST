"""Retire legacy RQ test jobs before removing Redis compatibility.

Revision ID: 20260731_0037
Revises: 20260731_0036
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260731_0037"
down_revision: str | None = "20260731_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        WITH retired AS (
            UPDATE job
            SET status = 'CANCELED',
                result_summary = jsonb_build_object(
                    'migration', '20260731_0037',
                    'reason', 'LEGACY_RQ_REMOVED',
                    'previous_status', status
                ),
                last_error_code = 'LEGACY_RQ_REMOVED',
                last_error_summary =
                    '旧版测试任务随 Redis/RQ 兼容层退役而明确取消',
                cancel_requested = false,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                terminal_at = now(),
                updated_at = now(),
                version = version + 1
            WHERE job_type = 'ADMIN_TEST'
              AND queue = 'maintenance'
              AND status = 'QUEUED'
            RETURNING id
        )
        UPDATE event_outbox
        SET status = 'DEAD',
            last_error_code = 'LEGACY_RQ_REMOVED',
            last_error_summary =
                '关联旧版测试任务已在 Redis/RQ 退役迁移中明确取消',
            locked_at = NULL,
            locked_by = NULL,
            updated_at = now()
        WHERE topic = 'jobs.control'
          AND status IN ('PENDING', 'DISPATCHING')
          AND aggregate_id IN (SELECT id::text FROM retired)
        """
    )


def downgrade() -> None:
    # Terminal task history is intentionally not made executable again.
    pass
