from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class SchedulerRuntimeState(Base):
    __tablename__ = "scheduler_runtime_state"
    __table_args__ = (
        CheckConstraint("consecutive_failures >= 0", name="failures_non_negative"),
    )

    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    clock_skew_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    automatic_scheduling_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    pause_reason: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
