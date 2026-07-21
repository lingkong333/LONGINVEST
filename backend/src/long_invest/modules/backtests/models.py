# ruff: noqa: E501
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
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


class BacktestTask(Base):
    __tablename__ = "backtest_task"
    __table_args__ = (
        CheckConstraint(
            "training_start_date <= training_end_date "
            "AND training_end_date < test_start_date "
            "AND test_start_date <= test_end_date",
            name="date_range_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    training_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    training_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    test_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    test_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    strategy_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="SINGLE_SECURITY"
    )
    universe_hash: Mapped[str | None] = mapped_column(String(64))
    source_code_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    environment_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    hysteresis_ratio: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=0
    )
    price_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNADJUSTED"
    )
    data_source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    parameter_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BacktestItem(Base):
    __tablename__ = "backtest_item"
    __table_args__ = (UniqueConstraint("task_id", "security_id", name="task_security"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_task.id", ondelete="RESTRICT"),
        nullable=False,
    )
    security_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))


class BacktestForecastSnapshot(Base):
    __tablename__ = "backtest_forecast_snapshot"
    __table_args__ = (
        UniqueConstraint("item_id", name="item"),
        CheckConstraint(
            "length(training_data_hash) = 64", name="training_data_hash_sha256"
        ),
        CheckConstraint(
            "length(source_code_hash) = 64", name="source_code_hash_sha256"
        ),
        CheckConstraint("length(parameter_hash) = 64", name="parameter_hash_sha256"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    training_data_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    low_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    low_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    high_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    high_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BacktestTargetAdjustment(Base):
    __tablename__ = "backtest_target_adjustment"
    __table_args__ = (
        UniqueConstraint("item_id", "event_date", name="item_event_date"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    adjustment_factor: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    before_low_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    before_low_watch: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=0
    )
    before_high_watch: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=0
    )
    before_high_strong: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=0
    )
    after_low_strong: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=0
    )
    after_low_watch: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=0
    )
    after_high_watch: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=0
    )
    after_high_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    data_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BacktestUniverseSnapshot(Base):
    __tablename__ = "backtest_universe_snapshot"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    task_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class BacktestOrder(Base):
    __tablename__ = "backtest_order"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class BacktestTrade(Base):
    __tablename__ = "backtest_trade"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    order_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)


class BacktestMetric(Base):
    __tablename__ = "backtest_metric"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class BacktestDailyResult(Base):
    __tablename__ = "backtest_daily_result"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
