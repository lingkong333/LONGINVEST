"""Create notification events, deliveries, and attempts."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260715_0006"
down_revision: str | None = "20260715_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("business_event_type", sa.String(length=100), nullable=False),
        sa.Column("business_event_id", sa.String(length=128), nullable=False),
        sa.Column("business_object_type", sa.String(length=64), nullable=False),
        sa.Column("business_object_id", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=True),
        sa.Column(
            "template_variables",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("eligibility_status", sa.String(length=32), nullable=False),
        sa.Column("suppression_reason", sa.String(length=100), nullable=True),
        sa.Column(
            "effective_channels",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("template_version", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "eligibility_status IN ('ELIGIBLE','SUPPRESSED')",
            name=op.f("ck_notification_event_eligibility_status_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('ELIGIBLE','SUPPRESSED','DISPATCHED','PARTIAL',"
            "'DELIVERED','FAILED','CANCELED')",
            name=op.f("ck_notification_event_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_event")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_notification_event_idempotency_key"),
        ),
    )
    op.create_index(
        "ix_notification_event_business_object",
        "notification_event",
        ["business_object_type", "business_object_id"],
    )
    op.create_index(
        "ix_notification_event_status_created",
        "notification_event",
        ["status", "created_at"],
    )

    op.create_table(
        "notification_delivery",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("target_fingerprint", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "unknown_compensation_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("deterministic_message_id", sa.String(length=200), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("ck_notification_delivery_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "channel IN ('WECOM','EMAIL')",
            name=op.f("ck_notification_delivery_channel_valid"),
        ),
        sa.CheckConstraint(
            "generation > 0",
            name=op.f("ck_notification_delivery_generation_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','RETRY_WAIT',"
            "'OUTCOME_UNKNOWN','FAILED','CANCELED','SKIPPED_DISABLED',"
            "'SKIPPED_INELIGIBLE')",
            name=op.f("ck_notification_delivery_status_valid"),
        ),
        sa.CheckConstraint(
            "unknown_compensation_count BETWEEN 0 AND 1",
            name=op.f("ck_notification_delivery_unknown_compensation_count_range"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["notification_event.id"],
            name=op.f("fk_notification_delivery_event_id_notification_event"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_delivery")),
        sa.UniqueConstraint(
            "event_id",
            "channel",
            "generation",
            name=op.f("uq_notification_delivery_event_id"),
        ),
        sa.UniqueConstraint(
            "lease_token",
            name=op.f("uq_notification_delivery_lease_token"),
        ),
    )
    op.create_index(
        "ix_notification_delivery_expired_lease",
        "notification_delivery",
        ["channel", "status", "lease_expires_at"],
    )
    op.create_index(
        "ix_notification_delivery_pending",
        "notification_delivery",
        ["channel", "status", "next_retry_at"],
    )

    op.create_table(
        "notification_delivery_attempt",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "possibly_delivered",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("response_summary", postgresql.JSONB(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_no > 0",
            name=op.f("ck_notification_delivery_attempt_attempt_no_positive"),
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name=op.f("ck_notification_delivery_attempt_duration_ms_nonnegative"),
        ),
        sa.CheckConstraint(
            "outcome IN ('SUCCESS','TEMPORARY_FAILURE','PERMANENT_FAILURE',"
            "'OUTCOME_UNKNOWN')",
            name=op.f("ck_notification_delivery_attempt_outcome_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["notification_delivery.id"],
            name=op.f(
                "fk_notification_delivery_attempt_delivery_id_notification_delivery"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_notification_delivery_attempt"),
        ),
        sa.UniqueConstraint(
            "delivery_id",
            "attempt_no",
            name=op.f("uq_notification_delivery_attempt_delivery_id"),
        ),
    )
    op.create_index(
        "ix_notification_delivery_attempt_delivery",
        "notification_delivery_attempt",
        ["delivery_id", "attempt_no"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_delivery_attempt_delivery",
        table_name="notification_delivery_attempt",
    )
    op.drop_table("notification_delivery_attempt")
    op.drop_index(
        "ix_notification_delivery_pending",
        table_name="notification_delivery",
    )
    op.drop_index(
        "ix_notification_delivery_expired_lease",
        table_name="notification_delivery",
    )
    op.drop_table("notification_delivery")
    op.drop_index(
        "ix_notification_event_status_created",
        table_name="notification_event",
    )
    op.drop_index(
        "ix_notification_event_business_object",
        table_name="notification_event",
    )
    op.drop_table("notification_event")
