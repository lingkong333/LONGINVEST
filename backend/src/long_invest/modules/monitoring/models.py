from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class ImmutableMonitoringFact:
    @staticmethod
    def _reject_mutation(_mapper: object, _connection: object, _target: object) -> None:
        raise TypeError("monitoring revisions are immutable")


class MonitorSubscription(Base):
    __tablename__ = "monitor_subscription"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CONFIGURING','ENABLED','PAUSED','ARCHIVED')",
            name="status_valid",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "(status = 'ARCHIVED') = (archived_at IS NOT NULL)",
            name="archive_consistent",
        ),
        Index(
            "uq_monitor_subscription_open_security",
            "security_id",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_subscription_revision.id", ondelete="RESTRICT"),
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MonitorSubscriptionRevision(ImmutableMonitoringFact, Base):
    __tablename__ = "monitor_subscription_revision"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "revision_no",
            name="uq_monitor_subscription_revision_number",
        ),
        UniqueConstraint(
            "subscription_id",
            "idempotency_key",
            name="uq_monitor_subscription_revision_idempotency",
        ),
        UniqueConstraint(
            "id",
            "subscription_id",
            name="uq_monitor_subscription_revision_identity",
        ),
        CheckConstraint("revision_no > 0", name="revision_positive"),
        CheckConstraint(
            "target_mode IN ('MANUAL','STRATEGY')", name="target_mode_valid"
        ),
        CheckConstraint(
            "notification_mode IN ('INHERIT','CUSTOM')",
            name="notification_mode_valid",
        ),
        CheckConstraint(
            "notification_channels IN "
            "('[]'::jsonb, '[\"WECOM\"]'::jsonb, '[\"EMAIL\"]'::jsonb, "
            '\'["WECOM", "EMAIL"]\'::jsonb)',
            name="notification_channels_valid",
        ),
        CheckConstraint(
            "notification_mode = 'CUSTOM' OR notification_channels = '[]'::jsonb",
            name="notification_inherit_channels_empty",
        ),
        CheckConstraint(
            "hysteresis_ratio >= 0 AND hysteresis_min >= 0",
            name="hysteresis_nonnegative",
        ),
        CheckConstraint("length(content_hash) = 64", name="content_hash_sha256"),
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
    schedule_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("monitor_schedule.id", ondelete="RESTRICT")
    )
    schedule_revision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_schedule_revision.id", ondelete="RESTRICT"),
    )
    target_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    target_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    strategy_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    hysteresis_ratio: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    hysteresis_min: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    notification_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    notification_channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScheduleOccurrence(Base):
    __tablename__ = "schedule_occurrence"
    __table_args__ = (
        UniqueConstraint(
            "occurrence_type",
            "schedule_id",
            "scheduled_at",
            name="uq_schedule_occurrence_scope",
        ),
        CheckConstraint(
            "status IN ('PENDING','CLAIMED','DISPATCHED','MISSED','FAILED')",
            name="status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    occurrence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schedule_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_schedule.id", ondelete="RESTRICT"),
        nullable=False,
    )
    schedule_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_schedule_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    subscription_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("job.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


event.listen(
    MonitorSubscriptionRevision,
    "before_update",
    MonitorSubscriptionRevision._reject_mutation,
)
event.listen(
    MonitorSubscriptionRevision,
    "before_delete",
    MonitorSubscriptionRevision._reject_mutation,
)
