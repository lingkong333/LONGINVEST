from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base

ZONES = "'UNKNOWN','STRONG_LOW','LOW','NORMAL','HIGH','STRONG_HIGH'"
REASONS = (
    "'SCHEDULED_QUOTE','MANUAL_CHECK','TARGET_ACTIVATED',"
    "'POSITION_BECAME_HOLDING','DATA_CORRECTION','STATE_RESET',"
    "'RECOVERY_REEVALUATION'"
)


class SignalState(Base):
    __tablename__ = "signal_state"
    __table_args__ = (
        UniqueConstraint("subscription_id", name="subscription"),
        CheckConstraint(f"zone IN ({ZONES})", name="zone_valid"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    subscription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_subscription.id", ondelete="RESTRICT"),
        nullable=False,
    )
    zone: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    last_price_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_price_version: Mapped[int | None] = mapped_column(Integer)
    last_target_revision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("target_revision.id", ondelete="RESTRICT")
    )
    last_target_version: Mapped[int | None] = mapped_column(Integer)
    last_position_version: Mapped[int | None] = mapped_column(Integer)
    last_evaluation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_event_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SignalEvaluation(Base):
    __tablename__ = "signal_evaluation"
    __table_args__ = (
        UniqueConstraint("subscription_id", "idempotency_key", name="idempotency"),
        CheckConstraint(f"reason IN ({REASONS})", name="reason_valid"),
        CheckConstraint(
            "result IN ('APPLIED','UNCHANGED','SKIPPED','SUPERSEDED')",
            name="result_valid",
        ),
        CheckConstraint(f"before_zone IN ({ZONES})", name="before_zone_valid"),
        CheckConstraint(f"after_zone IN ({ZONES})", name="after_zone_valid"),
        CheckConstraint(
            "subscription_version > 0 AND price_version > 0 "
            "AND target_version > 0 AND position_version >= 0",
            name="versions_positive",
        ),
        Index(
            "ix_signal_evaluation_subscription_created",
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
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    before_zone: Mapped[str] = mapped_column(String(16), nullable=False)
    after_zone: Mapped[str] = mapped_column(String(16), nullable=False)
    subscription_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("target_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    position_version: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    price_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price_version: Mapped[int] = mapped_column(Integer, nullable=False)
    hysteresis_applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    used_stale_target: Mapped[bool] = mapped_column(Boolean, nullable=False)
    skip_code: Mapped[str | None] = mapped_column(String(100))
    job_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("job.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SignalEvent(Base):
    __tablename__ = "signal_event"
    __table_args__ = (
        UniqueConstraint("evaluation_id", name="evaluation"),
        CheckConstraint(f"before_zone IN ({ZONES})", name="before_zone_valid"),
        CheckConstraint(f"after_zone IN ({ZONES})", name="after_zone_valid"),
        CheckConstraint("before_zone <> after_zone", name="real_transition"),
        CheckConstraint(f"reason IN ({REASONS})", name="reason_valid"),
        CheckConstraint(
            "notification_class IN ('LOW','LOW_CLEARED','HIGH','HIGH_CLEARED')",
            name="notification_class_valid",
        ),
        CheckConstraint(
            "target_version > 0 AND position_version >= 0 AND state_version > 0",
            name="versions_positive",
        ),
        Index("ix_signal_event_subscription_created", "subscription_id", "created_at"),
        Index("ix_signal_event_notification_eligible", "notification_eligible"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    subscription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_subscription.id", ondelete="RESTRICT"),
        nullable=False,
    )
    evaluation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("signal_evaluation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    before_zone: Mapped[str] = mapped_column(String(16), nullable=False)
    after_zone: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    price_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("target_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    position_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    notification_class: Mapped[str] = mapped_column(String(16), nullable=False)
    notification_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    suppression_reason: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
