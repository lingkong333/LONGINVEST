from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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


def _finite_numeric(*fields: str) -> str:
    return " AND ".join(
        f"{field} <> 'NaN'::numeric "
        f"AND {field} < 'Infinity'::numeric "
        f"AND {field} > '-Infinity'::numeric"
        for field in fields
    )


class BacktestTask(Base):
    __tablename__ = "backtest_task"
    __table_args__ = (
        CheckConstraint(
            "training_start_date <= training_end_date "
            "AND training_end_date < test_start_date "
            "AND test_start_date <= test_end_date",
            name="date_range_valid",
        ),
        CheckConstraint("mode IN ('SINGLE','WATCHLIST','MARKET')", name="mode_valid"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','PAUSING','PAUSED','SUCCEEDED',"
            "'PARTIAL','FAILED','CANCELING','CANCELED')",
            name="status_valid",
        ),
        CheckConstraint("execution_generation > 0", name="generation_positive"),
        CheckConstraint(
            "(strategy_version_id IS NOT NULL AND draft_id IS NULL "
            "AND draft_version IS NULL AND draft_source_code IS NULL) OR "
            "(strategy_version_id IS NULL AND draft_id IS NOT NULL "
            "AND draft_version > 0 AND draft_source_code IS NOT NULL "
            "AND length(trim(draft_source_code)) > 0)",
            name="strategy_source_valid",
        ),
        CheckConstraint(
            "universe_hash ~ '^[0-9a-f]{64}$' "
            "AND source_code_hash ~ '^[0-9a-f]{64}$' "
            "AND parameter_hash ~ '^[0-9a-f]{64}$' "
            "AND request_digest ~ '^[0-9a-f]{64}$'",
            name="hashes_sha256",
        ),
        CheckConstraint(
            "runner_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="runner_image_digest_sha256",
        ),
        CheckConstraint(
            "initial_capital > 0 AND initial_capital <> 'NaN'::numeric "
            "AND initial_capital < 'Infinity'::numeric",
            name="initial_capital_positive",
        ),
        CheckConstraint(
            "hysteresis_ratio >= 0 AND minimum_hysteresis >= 0",
            name="hysteresis_nonnegative",
        ),
        CheckConstraint(
            _finite_numeric(
                "hysteresis_ratio", "minimum_hysteresis", "initial_capital"
            ),
            name="numeric_finite",
        ),
        CheckConstraint(
            "length(trim(environment_version)) > 0 "
            "AND length(trim(strategy_api_version)) > 0 "
            "AND length(trim(rule_version)) > 0 "
            "AND length(trim(price_basis)) > 0 "
            "AND length(trim(data_source)) > 0",
            name="required_text_nonblank",
        ),
        Index("ix_backtest_task_status_created", "status", "created_at"),
        Index("ix_backtest_task_strategy_version", "strategy_version_id"),
        UniqueConstraint("idempotency_key", name="uq_backtest_task_idempotency_key"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    rerun_from_task_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_task.id", ondelete="RESTRICT"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    universe_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    training_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    training_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    test_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    test_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    strategy_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_version.id", ondelete="RESTRICT"),
    )
    draft_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("strategy_draft.id", ondelete="RESTRICT")
    )
    draft_version: Mapped[int | None] = mapped_column(Integer)
    draft_source_code: Mapped[str | None] = mapped_column(String)
    source_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parameter_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    environment_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    strategy_api_version: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    hysteresis_ratio: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    minimum_hysteresis: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    price_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    data_source: Mapped[str] = mapped_column(String(64), nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacktestUniverseSnapshot(Base):
    __tablename__ = "backtest_universe_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "task_id", name="uq_backtest_universe_snapshot_task_id"
        ),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_task.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class BacktestItem(Base):
    __tablename__ = "backtest_item"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "security_id",
            name="uq_backtest_item_task_id_security_id",
        ),
        CheckConstraint(
            "status IN ('PENDING','FETCHING_DATA','VALIDATING_DATA','FORECASTING',"
            "'FROZEN','SIMULATING','SAVING','SUCCEEDED','FAILED','SKIPPED','CANCELED')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'FAILED' AND failure_code IS NOT NULL) OR "
            "(status <> 'FAILED' AND failure_code IS NULL)",
            name="failure_consistent",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "(training_data_fetched_at IS NULL AND training_data_start_date IS NULL "
            "AND training_data_end_date IS NULL AND training_data_row_count IS NULL "
            "AND training_data_hash IS NULL AND training_price_basis IS NULL) OR "
            "(training_data_fetched_at IS NOT NULL "
            "AND training_data_start_date IS NOT NULL "
            "AND training_data_end_date IS NOT NULL "
            "AND training_data_start_date <= training_data_end_date "
            "AND training_data_row_count > 0 "
            "AND training_data_hash ~ '^[0-9a-f]{64}$' "
            "AND length(trim(training_price_basis)) > 0)",
            name="training_snapshot_consistent",
        ),
        CheckConstraint(
            "(test_data_fetched_at IS NULL AND test_data_start_date IS NULL "
            "AND test_data_end_date IS NULL AND test_data_row_count IS NULL "
            "AND test_data_hash IS NULL AND test_price_basis IS NULL) OR "
            "(test_data_fetched_at IS NOT NULL AND test_data_start_date IS NOT NULL "
            "AND test_data_end_date IS NOT NULL "
            "AND test_data_start_date <= test_data_end_date "
            "AND test_data_row_count > 0 "
            "AND test_data_hash ~ '^[0-9a-f]{64}$' "
            "AND length(trim(test_price_basis)) > 0)",
            name="test_snapshot_consistent",
        ),
        Index("ix_backtest_item_task_status", "task_id", "status"),
        Index("ix_backtest_item_security", "security_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_task.id", ondelete="RESTRICT"),
        nullable=False,
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    execution_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    training_data_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    training_data_start_date: Mapped[date | None] = mapped_column(Date)
    training_data_end_date: Mapped[date | None] = mapped_column(Date)
    training_data_row_count: Mapped[int | None] = mapped_column(Integer)
    training_data_hash: Mapped[str | None] = mapped_column(String(64))
    training_price_basis: Mapped[str | None] = mapped_column(String(32))
    test_data_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    test_data_start_date: Mapped[date | None] = mapped_column(Date)
    test_data_end_date: Mapped[date | None] = mapped_column(Date)
    test_data_row_count: Mapped[int | None] = mapped_column(Integer)
    test_data_hash: Mapped[str | None] = mapped_column(String(64))
    test_price_basis: Mapped[str | None] = mapped_column(String(32))


class BacktestControlCommand(Base):
    __tablename__ = "backtest_control_command"
    __table_args__ = (
        CheckConstraint(
            "action IN ('PAUSE','RESUME','CANCEL','RETRY_FAILED','RERUN')",
            name="action_valid",
        ),
        CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'", name="request_digest_sha256"
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_backtest_control_command_idempotency_key"
        ),
        Index("ix_backtest_control_command_task_created", "task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_task.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_task_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_task.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BacktestForecastSnapshot(Base):
    __tablename__ = "backtest_forecast_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "item_id", name="uq_backtest_forecast_snapshot_item_id"
        ),
        CheckConstraint(
            "training_start_date <= training_end_date AND training_row_count > 0",
            name="training_range_valid",
        ),
        CheckConstraint(
            "training_data_hash ~ '^[0-9a-f]{64}$' "
            "AND source_code_hash ~ '^[0-9a-f]{64}$' "
            "AND parameter_hash ~ '^[0-9a-f]{64}$'",
            name="hashes_sha256",
        ),
        CheckConstraint(
            "runner_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="runner_image_digest_sha256",
        ),
        CheckConstraint("training_fetched_at <= frozen_at", name="fetch_before_freeze"),
        CheckConstraint(
            "low_strong > 0 AND low_watch > 0 AND high_watch > 0 "
            "AND high_strong > 0 AND low_strong < low_watch "
            "AND low_watch < high_watch AND high_watch < high_strong",
            name="targets_ordered",
        ),
        CheckConstraint(
            _finite_numeric("low_strong", "low_watch", "high_watch", "high_strong"),
            name="numeric_finite",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    training_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    training_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    training_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    training_fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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
    environment_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    price_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BacktestAdjustmentSnapshot(Base):
    __tablename__ = "backtest_adjustment_snapshot"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_backtest_adjustment_snapshot_item_id"),
        CheckConstraint("start_date <= end_date", name="date_range_valid"),
        CheckConstraint("row_count >= 0", name="row_count_nonnegative"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256"
        ),
        CheckConstraint("fetched_at <= as_of", name="fetch_before_knowledge_cutoff"),
        CheckConstraint(
            "length(trim(source)) > 0 AND "
            "length(trim(provider_contract_version)) > 0",
            name="required_text_nonblank",
        ),
        CheckConstraint(
            "jsonb_typeof(entries) = 'array' "
            "AND jsonb_array_length(entries) = row_count",
            name="entries_consistent",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entries: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BacktestTargetAdjustment(Base):
    __tablename__ = "backtest_target_adjustment"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "event_date",
            name="uq_backtest_target_adjustment_item_id_event_date",
        ),
        CheckConstraint(
            "adjustment_factor > 0 AND adjustment_factor <> 'NaN'::numeric "
            "AND adjustment_factor < 'Infinity'::numeric",
            name="factor_positive",
        ),
        CheckConstraint(
            "before_low_strong > 0 AND before_low_strong < before_low_watch "
            "AND before_low_watch < before_high_watch "
            "AND before_high_watch < before_high_strong "
            "AND after_low_strong > 0 AND after_low_strong < after_low_watch "
            "AND after_low_watch < after_high_watch "
            "AND after_high_watch < after_high_strong",
            name="targets_ordered",
        ),
        CheckConstraint("data_hash ~ '^[0-9a-f]{64}$'", name="data_hash_sha256"),
        CheckConstraint(
            "published_at <= effective_at", name="publication_before_effective"
        ),
        CheckConstraint(
            _finite_numeric(
                "adjustment_factor",
                "before_low_strong",
                "before_low_watch",
                "before_high_watch",
                "before_high_strong",
                "after_low_strong",
                "after_low_watch",
                "after_high_watch",
                "after_high_strong",
            ),
            name="numeric_finite",
        ),
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
    before_low_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    before_high_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    before_high_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    after_low_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    after_low_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    after_high_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    after_high_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    data_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BacktestOrder(Base):
    __tablename__ = "backtest_order"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "signal_date",
            "direction",
            name="uq_backtest_order_item_id_signal_date_direction",
        ),
        CheckConstraint(
            "status IN ('PENDING','FILLED','UNFILLED_AT_END')",
            name="status_valid",
        ),
        CheckConstraint("direction IN ('BUY','SELL')", name="direction_valid"),
        CheckConstraint(
            "(status = 'FILLED' AND execute_date IS NOT NULL "
            "AND execute_date > signal_date AND execution_price > 0 "
            "AND quantity > 0) OR "
            "(status IN ('PENDING','UNFILLED_AT_END') "
            "AND execute_date IS NULL AND execution_price IS NULL "
            "AND quantity IS NULL)",
            name="execution_consistent",
        ),
        CheckConstraint(
            "cash_before >= 0 AND position_before >= 0",
            name="balances_nonnegative",
        ),
        CheckConstraint(
            "target_low_strong > 0 AND target_low_strong < target_low_watch "
            "AND target_low_watch < target_high_watch "
            "AND target_high_watch < target_high_strong",
            name="targets_ordered",
        ),
        CheckConstraint(
            "target_zone IN ('UNKNOWN','STRONG_LOW','LOW','NORMAL','HIGH',"
            "'STRONG_HIGH')",
            name="target_zone_valid",
        ),
        CheckConstraint(
            _finite_numeric(
                "execution_price",
                "quantity",
                "cash_before",
                "position_before",
                "target_low_strong",
                "target_low_watch",
                "target_high_watch",
                "target_high_strong",
            ),
            name="numeric_finite",
        ),
        Index("ix_backtest_order_item_status", "item_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    execute_date: Mapped[date | None] = mapped_column(Date)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    execution_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    cash_before: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    position_before: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    target_low_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_low_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_high_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_high_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_zone: Mapped[str] = mapped_column(String(16), nullable=False)


class BacktestTrade(Base):
    __tablename__ = "backtest_trade"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_backtest_trade_order_id"),
        CheckConstraint("direction IN ('BUY','SELL')", name="direction_valid"),
        CheckConstraint(
            "price > 0 AND quantity > 0 AND cash_after >= 0 "
            "AND position_after >= 0 AND round_trip_no > 0 "
            "AND (holding_trade_days IS NULL OR holding_trade_days >= 0) "
            "AND ((direction = 'SELL' AND holding_trade_days IS NOT NULL "
            "AND realized_return_amount IS NOT NULL "
            "AND realized_return_rate IS NOT NULL) OR "
            "(direction = 'BUY' AND holding_trade_days IS NULL "
            "AND realized_return_amount IS NULL AND realized_return_rate IS NULL))",
            name="values_valid",
        ),
        CheckConstraint(
            "target_low_strong > 0 AND target_low_strong < target_low_watch "
            "AND target_low_watch < target_high_watch "
            "AND target_high_watch < target_high_strong",
            name="targets_ordered",
        ),
        CheckConstraint(
            "target_zone IN ('UNKNOWN','STRONG_LOW','LOW','NORMAL','HIGH',"
            "'STRONG_HIGH')",
            name="target_zone_valid",
        ),
        CheckConstraint(
            _finite_numeric(
                "price",
                "quantity",
                "cash_after",
                "position_after",
                "target_low_strong",
                "target_low_watch",
                "target_high_watch",
                "target_high_strong",
                "realized_return_amount",
                "realized_return_rate",
            ),
            name="numeric_finite",
        ),
        Index(
            "ix_backtest_trade_item_execute_date", "item_id", "execute_date"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_order.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execute_date: Mapped[date] = mapped_column(Date, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    cash_after: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    position_after: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    target_low_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_low_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_high_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_high_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_zone: Mapped[str] = mapped_column(String(16), nullable=False)
    round_trip_no: Mapped[int] = mapped_column(Integer, nullable=False)
    holding_trade_days: Mapped[int | None] = mapped_column(Integer)
    realized_return_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    realized_return_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))


class BacktestMetric(Base):
    __tablename__ = "backtest_metric"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_backtest_metric_item_id"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256"),
        CheckConstraint(
            "completed_round_trips >= 0 AND winning_trades >= 0 "
            "AND losing_trades >= 0 AND breakeven_trades >= 0 "
            "AND winning_trades + losing_trades + breakeven_trades "
            "= completed_round_trips "
            "AND longest_holding_trade_days >= 0 AND unfilled_order_count >= 0",
            name="counts_nonnegative",
        ),
        CheckConstraint(
            "ending_equity >= 0 AND max_drawdown >= 0 AND max_drawdown <= 1 "
            "AND volatility >= 0 AND capital_exposure_ratio >= 0 "
            "AND capital_exposure_ratio <= 1",
            name="values_valid",
        ),
        CheckConstraint(
            "(completed_round_trips = 0 AND win_rate IS NULL) OR "
            "(completed_round_trips > 0 AND win_rate >= 0 AND win_rate <= 1)",
            name="win_rate_consistent",
        ),
        CheckConstraint(
            _finite_numeric(
                "ending_equity",
                "total_return",
                "realized_return",
                "annualized_return",
                "max_drawdown",
                "volatility",
                "sharpe_ratio",
                "win_rate",
                "average_trade_return",
                "maximum_trade_gain",
                "maximum_trade_loss",
                "average_holding_trade_days",
                "capital_exposure_ratio",
            ),
            name="numeric_finite",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ending_equity: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    total_return: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    realized_return: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    annualized_return: Mapped[Decimal] = mapped_column(Numeric(50, 8), nullable=False)
    max_drawdown: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volatility: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    sharpe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    completed_round_trips: Mapped[int] = mapped_column(Integer, nullable=False)
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False)
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False)
    breakeven_trades: Mapped[int] = mapped_column(Integer, nullable=False)
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    average_trade_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    maximum_trade_gain: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    maximum_trade_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    average_holding_trade_days: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    longest_holding_trade_days: Mapped[int] = mapped_column(Integer, nullable=False)
    capital_exposure_ratio: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False
    )
    open_position_at_end: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unfilled_order_count: Mapped[int] = mapped_column(Integer, nullable=False)


class BacktestDailyResult(Base):
    __tablename__ = "backtest_daily_result"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "trade_date",
            name="uq_backtest_daily_result_item_id_trade_date",
        ),
        CheckConstraint(
            "cash >= 0 AND position_quantity >= 0 AND close_price > 0 "
            "AND position_market_value >= 0 AND equity >= 0 "
            "AND equity = cash + position_market_value "
            "AND drawdown >= 0 AND drawdown <= 1",
            name="values_valid",
        ),
        CheckConstraint(
            "target_low_strong > 0 AND target_low_strong < target_low_watch "
            "AND target_low_watch < target_high_watch "
            "AND target_high_watch < target_high_strong",
            name="targets_ordered",
        ),
        CheckConstraint(
            "position_status IN ('FLAT','HOLDING')", name="position_status_valid"
        ),
        CheckConstraint(
            "zone IN ('UNKNOWN','STRONG_LOW','LOW','NORMAL','HIGH','STRONG_HIGH')",
            name="zone_valid",
        ),
        CheckConstraint(
            _finite_numeric(
                "cash",
                "position_quantity",
                "close_price",
                "position_market_value",
                "equity",
                "drawdown",
                "target_low_strong",
                "target_low_watch",
                "target_high_watch",
                "target_high_strong",
            ),
            name="numeric_finite",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("backtest_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    position_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    position_market_value: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False
    )
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    drawdown: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    target_low_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_low_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_high_watch: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    target_high_strong: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    zone: Mapped[str] = mapped_column(String(16), nullable=False)
    position_status: Mapped[str] = mapped_column(String(16), nullable=False)
