from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHING = "DISPATCHING"
    DISPATCHED = "DISPATCHED"
    DEAD = "DEAD"


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        CheckConstraint(
            "status IN ('PENDING','DISPATCHING','DISPATCHED','DEAD')",
            name="status_valid",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index(
            "ix_event_outbox_pending",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index("ix_event_outbox_lease", "status", "locked_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=OutboxStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rq_job_id: Mapped[str | None] = mapped_column(String(200))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )
