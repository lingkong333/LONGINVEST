from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base
from long_invest.platform.jobs.contracts import (
    JobItemStatus,
    JobRunStatus,
    JobStatus,
)


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("idempotency_scope", "idempotency_key"),
        CheckConstraint(
            "priority BETWEEN -100 AND 100",
            name="priority_range",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("soft_timeout_seconds > 0", name="soft_timeout_positive"),
        CheckConstraint(
            "hard_timeout_seconds >= soft_timeout_seconds "
            "AND hard_timeout_seconds <= 86400",
            name="hard_timeout_not_less_than_soft",
        ),
        CheckConstraint(
            "status IN ('PENDING_DISPATCH','QUEUED','RUNNING','WAITING_RETRY',"
            "'PAUSING','PAUSED','CANCEL_REQUESTED','SUCCEEDED','PARTIAL','FAILED',"
            "'TIMED_OUT','LOST','CANCELED','BLOCKED','REJECTED')",
            name="status_valid",
        ),
        Index("ix_job_status_created", "status", "created_at"),
        Index("ix_job_type_created", "job_type", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    business_object_type: Mapped[str | None] = mapped_column(String(64))
    business_object_id: Mapped[str | None] = mapped_column(String(128))
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=JobStatus.PENDING_DISPATCH,
    )
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    idempotency_scope: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64))
    soft_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=300,
        server_default="300",
    )
    hard_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=360,
        server_default="360",
    )
    progress: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    current_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "job_run.id",
            name="fk_job_current_run_id_job_run",
            use_alter=True,
        ),
    )
    current_fence_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
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
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRun(Base):
    __tablename__ = "job_run"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no"),
        UniqueConstraint("fence_token"),
        CheckConstraint("attempt_no > 0", name="attempt_positive"),
        CheckConstraint(
            "soft_timeout_seconds > 0",
            name="soft_timeout_positive",
        ),
        CheckConstraint(
            "hard_timeout_seconds >= soft_timeout_seconds",
            name="hard_timeout_not_less_than_soft",
        ),
        CheckConstraint(
            "status IN ('CLAIMED','STARTING','RUNNING','SUCCEEDED','FAILED',"
            "'TIMED_OUT','CANCELED','LOST','SUPERSEDED')",
            name="status_valid",
        ),
        Index("ix_job_run_active_heartbeat", "status", "heartbeat_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("job.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    fence_token: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        default=uuid4,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=JobRunStatus.CLAIMED,
    )
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    soft_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    hard_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_type: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(String(500))
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class JobItem(Base):
    __tablename__ = "job_item"
    __table_args__ = (
        UniqueConstraint("job_id", "item_key"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "status IN ('PENDING','FETCHING','VALIDATING','RUNNING','SAVING',"
            "'SUCCEEDED','FAILED','SKIPPED','CANCELED')",
            name="status_valid",
        ),
        Index("ix_job_item_job_status", "job_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("job.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=JobItemStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    result_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
