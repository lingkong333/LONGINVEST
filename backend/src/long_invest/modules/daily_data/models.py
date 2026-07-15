from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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


class DailyDataBatch(Base):
    __tablename__ = "daily_data_batch"
    __table_args__ = (
        Index(
            "uq_daily_batch_auto_scope",
            "trading_date",
            "universe_snapshot_id",
            unique=True,
            postgresql_where=text("parent_batch_id IS NULL"),
        ),
        UniqueConstraint("idempotency_key", name="uq_daily_batch_idempotency"),
        CheckConstraint("expected_count > 0", name="daily_batch_expected_positive"),
        CheckConstraint(
            "status IN ('PENDING','FETCHING','VALIDATING','COMMITTING',"
            "'SUCCEEDED','PARTIAL','FAILED')",
            name="daily_batch_status_valid",
        ),
        Index("ix_daily_batch_date_status", "trading_date", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    universe_snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    parent_batch_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("daily_data_batch.id", ondelete="RESTRICT"),
    )
    symbols: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DailyBarStage(Base):
    __tablename__ = "daily_bar_stage"
    __table_args__ = (
        UniqueConstraint("batch_id", "symbol", name="uq_daily_stage_symbol"),
        CheckConstraint(
            "status IN ('FETCHED','VALID','REVIEW_REQUIRED',"
            "'INVALID','MISSING','FAILED')",
            name="daily_stage_status_valid",
        ),
        Index("ix_daily_stage_batch_status", "batch_id", "status"),
        Index("ix_daily_stage_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("daily_data_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    security_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    missing_reason: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(100))
    quality_code: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class DailyBarUnadjusted(Base):
    __tablename__ = "daily_bar_unadjusted"
    __table_args__ = (
        PrimaryKeyConstraint(
            "security_id", "trade_date", name="pk_daily_bar_unadjusted"
        ),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="daily_bar_prices_positive",
        ),
        CheckConstraint(
            "high >= open AND high >= close AND high >= low AND "
            "low <= open AND low <= close AND low <= high",
            name="daily_bar_ohlc_valid",
        ),
        CheckConstraint(
            "volume >= 0 AND amount >= 0", name="daily_bar_quantities_nonnegative"
        ),
        Index("ix_daily_bar_symbol_date", "symbol", "trade_date"),
        {"postgresql_partition_by": "RANGE (trade_date)"},
    )

    security_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    previous_close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    data_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DailyBarRevision(Base):
    __tablename__ = "daily_bar_revision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["daily_bar_security_id", "daily_bar_trade_date"],
            ["daily_bar_unadjusted.security_id", "daily_bar_unadjusted.trade_date"],
            name="fk_daily_revision_bar",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "daily_bar_security_id",
            "daily_bar_trade_date",
            "revision_no",
            name="uq_daily_bar_revision_no",
        ),
        Index("ix_daily_revision_symbol_date", "symbol", "daily_bar_trade_date"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    daily_bar_security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    daily_bar_trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    old_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    new_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    changed_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DailyBatchMissingItem(Base):
    __tablename__ = "daily_batch_missing_item"
    __table_args__ = (
        UniqueConstraint("batch_id", "symbol", name="uq_daily_missing_symbol"),
        CheckConstraint(
            "reason IN ('SUSPENDED','NOT_YET_LISTED','DELISTED',"
            "'NOT_EXPECTED_TO_TRADE','UNEXPLAINED')",
            name="daily_missing_reason_valid",
        ),
        Index("ix_daily_missing_batch_explained", "batch_id", "explained"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("daily_data_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    security_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    explained: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
