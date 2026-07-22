"""dynamic settings and encrypted secrets

Revision ID: 20260722_0015
Revises: 20260722_0014
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260722_0015"
down_revision: str | Sequence[str] | None = "20260722_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_setting",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(64)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "system_setting_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "setting_key",
            sa.String(100),
            sa.ForeignKey("system_setting.key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("actor_user_id", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("setting_key", "version"),
    )
    op.create_table(
        "secret_value",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("ciphertext", sa.LargeBinary()),
        sa.Column("configured", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    settings = sa.table(
        "system_setting",
        sa.column("key", sa.String),
        sa.column("value", postgresql.JSONB),
        sa.column("schema_version", sa.Integer),
        sa.column("version", sa.Integer),
    )
    defaults = [
        {
            "key": "notification.policy.global",
            "value": {"enabled": True, "channels": []},
            "schema_version": 1,
            "version": 1,
        },
        {
            "key": "notification.policy.signals",
            "value": {"enabled": True, "channels": []},
            "schema_version": 1,
            "version": 1,
        },
        {
            "key": "notification.policy.system_alerts",
            "value": {"enabled": True, "channels": []},
            "schema_version": 1,
            "version": 1,
        },
        {
            "key": "notification.channel.wecom",
            "value": {"enabled": False, "timeout_seconds": 5.0},
            "schema_version": 1,
            "version": 1,
        },
        {
            "key": "notification.channel.email",
            "value": {
                "enabled": False,
                "smtp_host": "",
                "smtp_port": 465,
                "security": "SSL",
                "username": "",
                "sender": "",
                "recipients": [],
                "timeout_seconds": 10.0,
            },
            "schema_version": 1,
            "version": 1,
        },
    ]
    op.bulk_insert(settings, defaults)
    history = sa.table(
        "system_setting_history",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("setting_key", sa.String),
        sa.column("version", sa.Integer),
        sa.column("value", postgresql.JSONB),
        sa.column("reason", sa.String),
        sa.column("actor_user_id", sa.String),
        sa.column("request_id", sa.String),
    )
    op.bulk_insert(
        history,
        [
            {
                "id": uuid4(),
                "setting_key": item["key"],
                "version": 1,
                "value": item["value"],
                "reason": "系统初始配置",
                "actor_user_id": "system",
                "request_id": "migration-20260722-0015",
            }
            for item in defaults
        ],
    )
    op.execute("GRANT SELECT, UPDATE ON system_setting TO longinvest_app")
    op.execute("GRANT SELECT, INSERT ON system_setting_history TO longinvest_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON secret_value TO longinvest_app")


def downgrade() -> None:
    op.drop_table("secret_value")
    op.drop_table("system_setting_history")
    op.drop_table("system_setting")
