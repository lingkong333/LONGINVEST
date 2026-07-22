"""system alerts and severity notification policy

Revision ID: 20260722_0016
Revises: 20260722_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260722_0016"
down_revision: str | Sequence[str] | None = "20260722_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_alert",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("aggregation_key", sa.String(200), nullable=False, unique=True),
        sa.Column("alert_type", sa.String(100), nullable=False),
        sa.Column("object_type", sa.String(100), nullable=False),
        sa.Column("object_id", sa.String(128), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by_user_id", sa.String(64)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by_user_id", sa.String(64)),
        sa.Column("resolution_reason", sa.Text()),
        sa.Column("retry_job_type", sa.String(64)),
        sa.Column("retry_queue", sa.String(64)),
        sa.Column("retry_config", postgresql.JSONB()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "severity IN ('INFO','WARNING','ERROR','CRITICAL')",
            name="ck_system_alert_severity_valid",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','RESOLVED')",
            name="ck_system_alert_status_valid",
        ),
        sa.CheckConstraint(
            "occurrence_count > 0", name="ck_system_alert_occurrence_count_positive"
        ),
        sa.CheckConstraint("version > 0", name="ck_system_alert_version_positive"),
    )
    op.create_index(
        "ix_system_alert_status_last_seen", "system_alert", ["status", "last_seen_at"]
    )
    op.create_index(
        "ix_system_alert_type_object",
        "system_alert",
        ["alert_type", "object_type", "object_id"],
    )
    op.create_table(
        "system_alert_occurrence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_alert.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_event_id", sa.String(160), nullable=False, unique=True),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "severity IN ('INFO','WARNING','ERROR','CRITICAL')",
            name="ck_system_alert_occurrence_severity_valid",
        ),
    )
    op.create_index(
        "ix_system_alert_occurrence_alert",
        "system_alert_occurrence",
        ["alert_id", "occurred_at"],
    )
    op.create_table(
        "system_alert_action",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_alert.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("actor_user_id", sa.String(64)),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("job.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action IN ('OPENED','UPDATED','ESCALATED','REOPENED','ACKNOWLEDGED',"
            "'RESOLVED','AUTO_RESOLVED','RETRY_REQUESTED')",
            name="ck_system_alert_action_action_valid",
        ),
    )
    op.create_index(
        "ix_system_alert_action_alert",
        "system_alert_action",
        ["alert_id", "created_at"],
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON system_alert TO longinvest_app")
    op.execute(
        "GRANT SELECT, INSERT ON system_alert_occurrence, "
        "system_alert_action TO longinvest_app"
    )
    op.execute(
        sa.text("""
        UPDATE system_setting
        SET value = jsonb_build_object(
            'enabled', COALESCE((value->>'enabled')::boolean, true),
            'warning', '[]'::jsonb,
            'error', COALESCE(value->'channels', '[]'::jsonb),
            'critical', COALESCE(value->'channels', '[]'::jsonb),
            'recovered', COALESCE(value->'channels', '[]'::jsonb),
            'daily_unresolved', COALESCE(value->'channels', '[]'::jsonb)
        ), version = version + 1
        WHERE key = 'notification.policy.system_alerts' AND value ? 'channels'
    """)
    )
    op.execute(
        sa.text("""
        INSERT INTO system_setting_history
            (id, setting_key, version, value, reason, actor_user_id, request_id)
        SELECT gen_random_uuid(), key, version, value,
               '升级为按严重度区分的系统告警策略', 'system', 'migration-20260722-0016'
        FROM system_setting
        WHERE key = 'notification.policy.system_alerts'
          AND NOT EXISTS (
              SELECT 1 FROM system_setting_history h
              WHERE h.setting_key = system_setting.key
                AND h.version = system_setting.version
          )
    """)
    )


def downgrade() -> None:
    op.drop_table("system_alert_action")
    op.drop_table("system_alert_occurrence")
    op.drop_table("system_alert")
