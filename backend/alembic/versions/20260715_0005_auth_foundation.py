"""Create authentication users and sessions."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260715_0005"
down_revision: str | None = "20260714_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("password_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="ACTIVE", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_ip", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "password_version > 0",
            name=op.f("ck_app_user_password_version_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DISABLED')",
            name=op.f("ck_app_user_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("username", name=op.f("uq_app_user_username")),
    )
    op.create_index(
        "uq_app_user_singleton",
        "app_user",
        [sa.text("(1)")],
        unique=True,
    )

    op.create_table(
        "user_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("csrf_secret_digest", sa.String(length=64), nullable=False),
        sa.Column("password_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_request_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_user_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent_summary", sa.String(length=255), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default="ACTIVE", nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "absolute_expires_at > created_at",
            name=op.f("ck_user_session_absolute_expiry_after_creation"),
        ),
        sa.CheckConstraint(
            "password_version > 0",
            name=op.f("ck_user_session_password_version_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','EXPIRED_IDLE','EXPIRED_ABSOLUTE','REVOKED',"
            "'PASSWORD_CHANGED','USER_DISABLED')",
            name=op.f("ck_user_session_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_user_session_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_session")),
        sa.UniqueConstraint("token_digest", name=op.f("uq_user_session_token_digest")),
    )
    op.create_index(
        "ix_user_session_expiry",
        "user_session",
        ["status", "idle_expires_at"],
    )
    op.create_index(
        "ix_user_session_user_status",
        "user_session",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_session_user_status", table_name="user_session")
    op.drop_index("ix_user_session_expiry", table_name="user_session")
    op.drop_table("user_session")
    op.drop_index("uq_app_user_singleton", table_name="app_user")
    op.drop_table("app_user")
