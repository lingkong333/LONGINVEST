from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class QfqDataset(Base):
    __tablename__ = "qfq_dataset"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "version", name="uq_qfq_dataset_security_version"
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "requested_start <= requested_end AND as_of_date = requested_end",
            name="requested_window_valid",
        ),
        CheckConstraint(
            "actual_start <= actual_end "
            "AND actual_start >= requested_start "
            "AND actual_end <= requested_end",
            name="actual_window_valid",
        ),
        CheckConstraint("row_count > 0", name="row_count_positive"),
        CheckConstraint("length(checksum) = 64", name="checksum_sha256"),
        CheckConstraint(
            "anchor_close > 0 AND anchor_date = actual_end AND actual_end = as_of_date",
            name="anchor_valid",
        ),
        CheckConstraint(
            "lifecycle IN ('STAGING','CURRENT','SUPERSEDED')",
            name="lifecycle_valid",
        ),
        CheckConstraint(
            "(lifecycle = 'STAGING' AND activated_at IS NULL "
            "AND superseded_at IS NULL) "
            "OR (lifecycle = 'CURRENT' AND activated_at IS NOT NULL "
            "AND superseded_at IS NULL) "
            "OR (lifecycle = 'SUPERSEDED' AND activated_at IS NOT NULL "
            "AND superseded_at IS NOT NULL)",
            name="lifecycle_timestamps_consistent",
        ),
        CheckConstraint("freshness IN ('FRESH','STALE')", name="freshness_valid"),
        CheckConstraint(
            "(freshness = 'FRESH' AND stale_reason IS NULL) "
            "OR (freshness = 'STALE' AND stale_reason IS NOT NULL)",
            name="freshness_reason_consistent",
        ),
        Index("ix_qfq_dataset_security_lifecycle", "security_id", "lifecycle"),
        Index(
            "uq_qfq_dataset_current_security",
            "security_id",
            unique=True,
            postgresql_where=text("lifecycle = 'CURRENT'"),
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
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_start: Mapped[date] = mapped_column(Date, nullable=False)
    requested_end: Mapped[date] = mapped_column(Date, nullable=False)
    actual_start: Mapped[date] = mapped_column(Date, nullable=False)
    actual_end: Mapped[date] = mapped_column(Date, nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_date: Mapped[date] = mapped_column(Date, nullable=False)
    anchor_close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)
    freshness: Mapped[str] = mapped_column(String(16), nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QfqDatasetBar(Base):
    __tablename__ = "qfq_dataset_bar"
    __table_args__ = (
        PrimaryKeyConstraint("dataset_id", "trade_date"),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="prices_positive",
        ),
        CheckConstraint(
            "high >= open AND high >= close AND high >= low "
            "AND low <= open AND low <= close AND low <= high",
            name="ohlc_valid",
        ),
        CheckConstraint("volume >= 0 AND amount >= 0", name="quantities_nonnegative"),
        Index("ix_qfq_dataset_bar_trade_date", "trade_date"),
    )

    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("qfq_dataset.id", ondelete="CASCADE"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)


class QfqRefreshRun(Base):
    __tablename__ = "qfq_refresh_run"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "idempotency_key", name="uq_qfq_refresh_run_idempotency"
        ),
        UniqueConstraint(
            "security_id",
            "request_hash",
            name="uq_qfq_refresh_run_security_request_hash",
        ),
        CheckConstraint(
            "requested_start <= requested_end AND as_of_date = requested_end",
            name="window_valid",
        ),
        CheckConstraint("input_daily_version > 0", name="input_daily_version_positive"),
        CheckConstraint(
            "jsonb_typeof(expected_trade_dates) = 'array' "
            "AND jsonb_array_length(expected_trade_dates) > 0",
            name="expected_dates_nonempty",
        ),
        CheckConstraint(
            "status IN ('PENDING','FETCHING','VALIDATING','COMMITTING',"
            "'SUCCEEDED','FAILED','TIMED_OUT','SUPERSEDED')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'SUCCEEDED' AND activated_dataset_id IS NOT NULL "
            "AND candidate_dataset_id IS NOT NULL "
            "AND candidate_dataset_id = activated_dataset_id "
            "AND row_count IS NOT NULL AND checksum IS NOT NULL "
            "AND error_code IS NULL) "
            "OR (status IN ('FAILED','TIMED_OUT','SUPERSEDED') "
            "AND candidate_dataset_id IS NULL AND activated_dataset_id IS NULL "
            "AND row_count IS NULL AND checksum IS NULL "
            "AND error_code IS NOT NULL) "
            "OR (status IN ('PENDING','FETCHING','VALIDATING','COMMITTING') "
            "AND candidate_dataset_id IS NULL AND activated_dataset_id IS NULL "
            "AND row_count IS NULL AND checksum IS NULL AND error_code IS NULL)",
            name="result_consistent",
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED','FAILED','TIMED_OUT','SUPERSEDED') "
            "AND completed_at IS NOT NULL) "
            "OR (status IN ('PENDING','FETCHING','VALIDATING','COMMITTING') "
            "AND completed_at IS NULL)",
            name="completion_consistent",
        ),
        CheckConstraint(
            "row_count IS NULL OR row_count > 0", name="row_count_positive"
        ),
        CheckConstraint(
            "checksum IS NULL OR length(checksum) = 64", name="checksum_sha256"
        ),
        Index("ix_qfq_refresh_run_security_created", "security_id", "created_at"),
        Index("ix_qfq_refresh_run_status_updated", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("job.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_start: Mapped[date] = mapped_column(Date, nullable=False)
    requested_end: Mapped[date] = mapped_column(Date, nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_trade_dates: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    input_daily_version: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_dataset_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("qfq_dataset.id", ondelete="RESTRICT")
    )
    activated_dataset_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("qfq_dataset.id", ondelete="RESTRICT")
    )
    row_count: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
