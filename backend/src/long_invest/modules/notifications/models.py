from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from long_invest.modules.notifications.contracts import (
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.security import validate_notification_payload
from long_invest.platform.database.base import Base


class NotificationEvent(Base):
    __tablename__ = "notification_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ELIGIBLE','SUPPRESSED','DISPATCHED','PARTIAL',"
            "'DELIVERED','FAILED','CANCELED')",
            name="status_valid",
        ),
        CheckConstraint(
            "eligibility_status IN ('ELIGIBLE','SUPPRESSED')",
            name="eligibility_status_valid",
        ),
        Index("ix_notification_event_status_created", "status", "created_at"),
        Index(
            "ix_notification_event_business_object",
            "business_object_type",
            "business_object_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    business_event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    business_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    business_object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    business_object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(20))
    template_variables: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=NotificationEventStatus.ELIGIBLE,
    )
    eligibility_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=NotificationEventStatus.ELIGIBLE,
    )
    suppression_reason: Mapped[str | None] = mapped_column(String(100))
    effective_channels: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    template_version: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        unique=True,
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    @validates("template_variables")
    def validate_template_variables(
        self,
        _key: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        return validate_notification_payload(value)


class NotificationDelivery(Base):
    __tablename__ = "notification_delivery"
    __table_args__ = (
        UniqueConstraint("event_id", "channel", "generation"),
        CheckConstraint("generation > 0", name="generation_positive"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "unknown_compensation_count BETWEEN 0 AND 1",
            name="unknown_compensation_count_range",
        ),
        CheckConstraint(
            "channel IN ('WECOM','EMAIL')",
            name="channel_valid",
        ),
        CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','RETRY_WAIT',"
            "'OUTCOME_UNKNOWN','FAILED','CANCELED','SKIPPED_DISABLED',"
            "'SKIPPED_INELIGIBLE')",
            name="status_valid",
        ),
        Index(
            "ix_notification_delivery_pending",
            "channel",
            "status",
            "next_retry_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notification_event.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=NotificationDeliveryStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    unknown_compensation_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    deterministic_message_id: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class NotificationDeliveryAttempt(Base):
    __tablename__ = "notification_delivery_attempt"
    __table_args__ = (
        UniqueConstraint("delivery_id", "attempt_no"),
        CheckConstraint("attempt_no > 0", name="attempt_no_positive"),
        CheckConstraint("duration_ms >= 0", name="duration_ms_nonnegative"),
        CheckConstraint(
            "outcome IN ('SUCCESS','TEMPORARY_FAILURE','PERMANENT_FAILURE',"
            "'OUTCOME_UNKNOWN')",
            name="outcome_valid",
        ),
        Index(
            "ix_notification_delivery_attempt_delivery",
            "delivery_id",
            "attempt_no",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    delivery_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("notification_delivery.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    possibly_delivered: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    response_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    @validates("response_summary")
    def validate_response_summary(
        self,
        _key: str,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        return validate_notification_payload(value)
