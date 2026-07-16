from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class ImmutableScheduleFact:
    @staticmethod
    def _reject_mutation(_mapper: Any, _connection: Any, _target: Any) -> None:
        raise TypeError("schedule revisions are immutable")


class MonitorSchedule(Base):
    __tablename__ = "monitor_schedule"
    __table_args__ = (CheckConstraint("version > 0", name="version_positive"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_schedule_revision.id", ondelete="RESTRICT"),
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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


class MonitorScheduleRevision(ImmutableScheduleFact, Base):
    __tablename__ = "monitor_schedule_revision"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id", "revision_no", name="uq_monitor_schedule_revision_number"
        ),
        UniqueConstraint(
            "schedule_id",
            "idempotency_key",
            name="uq_monitor_schedule_revision_idempotency",
        ),
        CheckConstraint("revision_no > 0", name="revision_positive"),
        CheckConstraint("length(content_hash) = 64", name="content_hash_sha256"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    schedule_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitor_schedule.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    times: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Shanghai"
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


event.listen(
    MonitorScheduleRevision,
    "before_update",
    MonitorScheduleRevision._reject_mutation,
)
event.listen(
    MonitorScheduleRevision,
    "before_delete",
    MonitorScheduleRevision._reject_mutation,
)
