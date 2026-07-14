"""Create reliable job and outbox storage."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260714_0004"
down_revision: str | None = "20260714_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("business_object_type", sa.String(length=64), nullable=True),
        sa.Column("business_object_id", sa.String(length=128), nullable=True),
        sa.Column("queue", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "config_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_scope", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=True),
        sa.Column(
            "progress",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result_summary", postgresql.JSONB(), nullable=True),
        sa.Column("current_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("current_fence_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "priority BETWEEN -100 AND 100",
            name=op.f("ck_job_priority_range"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_DISPATCH','QUEUED','RUNNING','WAITING_RETRY',"
            "'PAUSING','PAUSED','CANCEL_REQUESTED','SUCCEEDED','PARTIAL','FAILED',"
            "'TIMED_OUT','LOST','CANCELED','BLOCKED','REJECTED')",
            name=op.f("ck_job_status_valid"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_job_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
        sa.UniqueConstraint(
            "idempotency_scope",
            "idempotency_key",
            name=op.f("uq_job_idempotency_scope"),
        ),
    )
    op.create_index("ix_job_status_created", "job", ["status", "created_at"])
    op.create_index("ix_job_type_created", "job", ["job_type", "created_at"])

    op.create_table(
        "job_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("fence_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("soft_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("hard_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("exit_type", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_no > 0",
            name=op.f("ck_job_run_attempt_positive"),
        ),
        sa.CheckConstraint(
            "hard_timeout_seconds >= soft_timeout_seconds",
            name=op.f("ck_job_run_hard_timeout_not_less_than_soft"),
        ),
        sa.CheckConstraint(
            "soft_timeout_seconds > 0",
            name=op.f("ck_job_run_soft_timeout_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('CLAIMED','STARTING','RUNNING','SUCCEEDED','FAILED',"
            "'TIMED_OUT','CANCELED','LOST','SUPERSEDED')",
            name=op.f("ck_job_run_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_run_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_run")),
        sa.UniqueConstraint("fence_token", name=op.f("uq_job_run_fence_token")),
        sa.UniqueConstraint(
            "job_id",
            "attempt_no",
            name=op.f("uq_job_run_job_id"),
        ),
    )
    op.create_index(
        "ix_job_run_active_heartbeat",
        "job_run",
        ["status", "heartbeat_at"],
    )
    op.create_foreign_key(
        "fk_job_current_run_id_job_run",
        "job",
        "job_run",
        ["current_run_id"],
        ["id"],
    )

    op.create_table(
        "job_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result_ref", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_job_item_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','FETCHING','VALIDATING','RUNNING','SAVING',"
            "'SUCCEEDED','FAILED','SKIPPED','CANCELED')",
            name=op.f("ck_job_item_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_item_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_item")),
        sa.UniqueConstraint(
            "job_id",
            "item_key",
            name=op.f("uq_job_item_job_id"),
        ),
    )
    op.create_index("ix_job_item_job_status", "job_item", ["job_id", "status"])

    op.create_table(
        "event_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("queue", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rq_job_id", sa.String(length=200), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_event_outbox_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','DISPATCHING','DISPATCHED','DEAD')",
            name=op.f("ck_event_outbox_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_event_outbox_dedupe_key")),
    )
    op.create_index(
        "ix_event_outbox_lease",
        "event_outbox",
        ["status", "locked_at"],
    )
    op.create_index(
        "ix_event_outbox_pending",
        "event_outbox",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_event_outbox_pending", table_name="event_outbox")
    op.drop_index("ix_event_outbox_lease", table_name="event_outbox")
    op.drop_table("event_outbox")
    op.drop_index("ix_job_item_job_status", table_name="job_item")
    op.drop_table("job_item")
    op.drop_constraint("fk_job_current_run_id_job_run", "job", type_="foreignkey")
    op.drop_index("ix_job_run_active_heartbeat", table_name="job_run")
    op.drop_table("job_run")
    op.drop_index("ix_job_type_created", table_name="job")
    op.drop_index("ix_job_status_created", table_name="job")
    op.drop_table("job")
