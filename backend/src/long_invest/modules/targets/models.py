from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class TargetRevision(Base):
    __tablename__ = "target_revision"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "revision_no", name="revision_number"
        ),
        UniqueConstraint(
            "subscription_id", "idempotency_key", name="idempotency"
        ),
        CheckConstraint("revision_no > 0", name="revision_positive"),
        CheckConstraint(
            "source IN ('MANUAL','RESTORED')", name="source_valid"
        ),
        CheckConstraint(
            "0 < low_strong AND low_strong < low_watch "
            "AND low_watch < high_watch AND high_watch < high_strong",
            name="values_ordered",
        ),
        CheckConstraint("length(content_hash) = 64", name="content_hash_sha256"),
        CheckConstraint(
            "source_code_hash IS NULL OR length(source_code_hash) = 64",
            name="source_code_hash_sha256",
        ),
        CheckConstraint(
            "data_version IS NULL OR data_version > 0", name="data_version_positive"
        ),
        Index(
            "ix_target_revision_subscription_created",
            "subscription_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    subscription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_subscription.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    low_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    low_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    high_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    high_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_revision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("target_revision.id", ondelete="RESTRICT"),
    )
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    strategy_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    parameter_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    data_version: Mapped[int | None] = mapped_column(Integer)
    source_code_hash: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    large_change_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trusted_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubscriptionTargetBinding(Base):
    __tablename__ = "subscription_target_binding"
    __table_args__ = (
        UniqueConstraint("subscription_id", name="subscription"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "status IN ('READY','STALE','CALCULATING','REVIEW_REQUIRED',"
            "'ACTIVATING','FAILED','MISSING')",
            name="status_valid",
        ),
        Index("ix_subscription_target_binding_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    subscription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_subscription.id", ondelete="RESTRICT"),
        nullable=False,
    )
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("target_revision.id", ondelete="RESTRICT"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
