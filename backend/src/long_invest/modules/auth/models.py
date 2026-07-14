from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.modules.auth.contracts import SessionStatus, UserStatus
from long_invest.platform.database.base import Base


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("username"),
        CheckConstraint("password_version > 0", name="password_version_positive"),
        CheckConstraint(
            "status IN ('ACTIVE','DISABLED')",
            name="status_valid",
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("password_version", 1)
        kwargs.setdefault("status", UserStatus.ACTIVE)
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    password_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[str | None] = mapped_column(String(64))


class UserSession(Base):
    __tablename__ = "user_session"
    __table_args__ = (
        UniqueConstraint("token_digest"),
        CheckConstraint("password_version > 0", name="password_version_positive"),
        CheckConstraint(
            "status IN ('ACTIVE','EXPIRED_IDLE','EXPIRED_ABSOLUTE','REVOKED',"
            "'PASSWORD_CHANGED','USER_DISABLED')",
            name="status_valid",
        ),
        CheckConstraint(
            "absolute_expires_at > created_at",
            name="absolute_expiry_after_creation",
        ),
        Index("ix_user_session_user_status", "user_id", "status"),
        Index("ix_user_session_expiry", "status", "idle_expires_at"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("status", SessionStatus.ACTIVE)
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_secret_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    password_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_request_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_user_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    idle_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent_summary: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SessionStatus.ACTIVE,
        server_default=SessionStatus.ACTIVE,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(255))
