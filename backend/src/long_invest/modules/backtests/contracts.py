from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from long_invest.modules.market_data.contracts import AdjustmentTimelineSnapshot
from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.targets.contracts import TargetValues
from long_invest.platform.json_snapshot import freeze_json_mapping, thaw_json_value
from long_invest.platform.validation import Sha256Hex


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestMode(StrEnum):
    SINGLE = "SINGLE"
    WATCHLIST = "WATCHLIST"
    MARKET = "MARKET"


class BacktestTaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELING = "CANCELING"
    CANCELED = "CANCELED"


class BacktestItemStatus(StrEnum):
    PENDING = "PENDING"
    FETCHING_DATA = "FETCHING_DATA"
    VALIDATING_DATA = "VALIDATING_DATA"
    FORECASTING = "FORECASTING"
    FROZEN = "FROZEN"
    SIMULATING = "SIMULATING"
    SAVING = "SAVING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELED = "CANCELED"


class BacktestAction(StrEnum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"
    RETRY_FAILED = "RETRY_FAILED"
    RERUN = "RERUN"


class BacktestErrorCode(StrEnum):
    BACKTEST_DATE_RANGE_INVALID = "BACKTEST_DATE_RANGE_INVALID"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    TRAINING_DATA_INVALID = "TRAINING_DATA_INVALID"
    TEST_DATA_INVALID = "TEST_DATA_INVALID"
    STRATEGY_FORECAST_TIMEOUT = "STRATEGY_FORECAST_TIMEOUT"
    STRATEGY_TARGET_INVALID = "STRATEGY_TARGET_INVALID"
    TEST_DATA_EXPOSED_TO_STRATEGY = "TEST_DATA_EXPOSED_TO_STRATEGY"
    TARGET_REFORECAST_FORBIDDEN = "TARGET_REFORECAST_FORBIDDEN"
    ADJUSTMENT_DATA_UNAVAILABLE = "ADJUSTMENT_DATA_UNAVAILABLE"
    PRICE_BASIS_MISMATCH = "PRICE_BASIS_MISMATCH"
    BACKTEST_RESULT_SAVE_FAILED = "BACKTEST_RESULT_SAVE_FAILED"


class BacktestDateRange(StrictContract):
    training_start_date: date
    training_end_date: date
    test_start_date: date
    test_end_date: date

    @model_validator(mode="after")
    def validate_order(self) -> BacktestDateRange:
        if not (
            self.training_start_date
            <= self.training_end_date
            < self.test_start_date
            <= self.test_end_date
        ):
            raise ValueError("training and test ranges must not overlap")
        return self


class BacktestSignalInput(StrictContract):
    security_id: UUID
    trade_date: date
    close_price: Decimal = Field(gt=0)
    targets: TargetValues


class BacktestPositionStatus(StrEnum):
    FLAT = "FLAT"
    HOLDING = "HOLDING"


class BacktestOrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    UNFILLED_AT_END = "UNFILLED_AT_END"


class BacktestOrderDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class BacktestSignalRuleInput(BacktestSignalInput):
    previous_zone: SignalZone
    position_status: BacktestPositionStatus
    hysteresis_ratio: Decimal = Field(ge=0)
    minimum_hysteresis: Decimal = Field(ge=0)


class BacktestSignalRuleResult(StrictContract):
    zone: SignalZone


class BacktestSignalRulePort(Protocol):
    def evaluate(self, signal: BacktestSignalRuleInput) -> BacktestSignalRuleResult: ...


class BacktestUniverseEntry(StrictContract):
    security_id: UUID
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=100)


class BacktestCreateRequest(StrictContract):
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    date_range: BacktestDateRange
    strategy_version_id: UUID | None = None
    draft_id: UUID | None = None
    draft_version: int | None = Field(default=None, ge=1)
    strategy_metadata: Mapping[str, Any] | None = None
    parameter_schema: Mapping[str, Any] | None = None
    parameter_snapshot: Mapping[str, Any]
    initial_capital: Decimal = Field(gt=0)

    @field_validator(
        "parameter_snapshot", "strategy_metadata", "parameter_schema"
    )
    @classmethod
    def freeze_creation_mapping(
        cls, value: Mapping[str, Any] | None
    ) -> Mapping[str, Any] | None:
        return freeze_json_mapping(value) if value is not None else None

    @model_validator(mode="after")
    def validate_strategy_choice(self) -> BacktestCreateRequest:
        has_version = self.strategy_version_id is not None
        has_draft = self.draft_id is not None or self.draft_version is not None
        if has_version == has_draft:
            raise ValueError("choose one strategy version or draft")
        if has_draft and (
            self.draft_id is None
            or self.draft_version is None
        ):
            raise ValueError("draft backtest requires id and version")
        if has_version and (
            self.strategy_metadata is not None or self.parameter_schema is not None
        ):
            raise ValueError("published strategy facts are resolved by the server")
        return self


class BacktestCreationSnapshotPort(Protocol):
    async def resolve_creation_snapshot(
        self, *, task_id: UUID, request: BacktestCreateRequest
    ) -> BacktestTaskSnapshot: ...


class BacktestStrategyExecution(StrictContract):
    strategy_id: UUID
    source_code: str = Field(min_length=1)


class BacktestStrategyExecutionPort(Protocol):
    async def resolve_execution(
        self, task: BacktestTaskSnapshot
    ) -> BacktestStrategyExecution: ...


class BacktestTaskSnapshot(StrictContract):
    id: UUID
    mode: BacktestMode
    universe_snapshot: tuple[BacktestUniverseEntry, ...]
    universe_hash: Sha256Hex
    date_range: BacktestDateRange
    strategy_version_id: UUID | None
    draft_id: UUID | None
    draft_version: int | None = Field(ge=1)
    draft_source_code: str | None
    source_code_hash: Sha256Hex
    strategy_metadata: Mapping[str, Any]
    parameter_schema: Mapping[str, Any]
    parameter_snapshot: Mapping[str, Any]
    parameter_hash: Sha256Hex
    environment_version: str = Field(min_length=1)
    runner_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    strategy_api_version: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    hysteresis_ratio: Decimal = Field(ge=0)
    minimum_hysteresis: Decimal = Field(ge=0)
    initial_capital: Decimal = Field(gt=0)
    price_basis: str = Field(min_length=1)
    data_source: str = Field(min_length=1)

    @field_validator("parameter_snapshot", "strategy_metadata", "parameter_schema")
    @classmethod
    def freeze_parameters(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json_mapping(value)

    @field_serializer("parameter_snapshot", "strategy_metadata", "parameter_schema")
    def serialize_parameters(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def validate_frozen_scope_and_strategy(self) -> BacktestTaskSnapshot:
        if not self.universe_snapshot:
            raise ValueError("backtest universe must not be empty")
        security_ids = [entry.security_id for entry in self.universe_snapshot]
        symbols = [entry.symbol for entry in self.universe_snapshot]
        if len(set(security_ids)) != len(security_ids) or len(set(symbols)) != len(
            symbols
        ):
            raise ValueError("backtest universe must not contain duplicates")
        if self.mode is BacktestMode.SINGLE and len(self.universe_snapshot) != 1:
            raise ValueError("single backtest must contain exactly one security")
        has_version = self.strategy_version_id is not None
        has_draft = any(
            value is not None
            for value in (self.draft_id, self.draft_version, self.draft_source_code)
        )
        if has_version == has_draft:
            raise ValueError("choose one published version or frozen draft source")
        if self.draft_source_code is not None and not self.draft_source_code.strip():
            raise ValueError("draft source code must not be blank")
        if has_draft and (
            self.draft_id is None
            or self.draft_version is None
            or self.draft_source_code is None
        ):
            raise ValueError("frozen draft requires id, version, and source code")
        return self


class BacktestForecastSnapshotView(StrictContract):
    item_id: UUID
    training_start_date: date
    training_end_date: date
    training_row_count: int = Field(gt=0)
    training_fetched_at: AwareDatetime
    training_data_hash: Sha256Hex
    source_code_hash: Sha256Hex
    parameter_hash: Sha256Hex
    values: TargetValues
    diagnostics: Mapping[str, Any]
    environment_version: str = Field(min_length=1)
    runner_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    price_basis: str = Field(min_length=1)
    frozen_at: AwareDatetime

    @field_validator("diagnostics")
    @classmethod
    def freeze_diagnostics(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json_mapping(value)

    @field_serializer("diagnostics")
    def serialize_diagnostics(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def validate_training_snapshot(self) -> BacktestForecastSnapshotView:
        if self.training_start_date > self.training_end_date:
            raise ValueError("training range is invalid")
        if self.training_fetched_at > self.frozen_at:
            raise ValueError("training data must be fetched before forecast is frozen")
        return self


class BacktestTestDataSnapshotView(StrictContract):
    item_id: UUID
    fetched_at: AwareDatetime
    start_date: date
    end_date: date
    row_count: int = Field(gt=0)
    data_hash: Sha256Hex
    price_basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> BacktestTestDataSnapshotView:
        if self.start_date > self.end_date:
            raise ValueError("test data range is invalid")
        return self


class BacktestTaskDetailView(StrictContract):
    task_snapshot: BacktestTaskSnapshot
    training_snapshots: tuple[BacktestForecastSnapshotView, ...]
    test_snapshots: tuple[BacktestTestDataSnapshotView, ...]

    @model_validator(mode="after")
    def validate_data_snapshot_pairs(self) -> BacktestTaskDetailView:
        training_ids = [snapshot.item_id for snapshot in self.training_snapshots]
        test_ids = [snapshot.item_id for snapshot in self.test_snapshots]
        if not training_ids or len(set(training_ids)) != len(training_ids):
            raise ValueError("training snapshots must be nonempty and unique")
        if not test_ids or len(set(test_ids)) != len(test_ids):
            raise ValueError("test snapshots must be nonempty and unique")
        if set(training_ids) != set(test_ids):
            raise ValueError("training and test snapshots must describe the same items")
        return self


class BacktestTargetAdjustmentView(StrictContract):
    item_id: UUID
    event_date: date
    before_values: TargetValues
    after_values: TargetValues
    adjustment_factor: Decimal = Field(gt=0)
    source: str = Field(min_length=1)
    data_hash: Sha256Hex
    published_at: AwareDatetime
    effective_at: AwareDatetime

    @model_validator(mode="after")
    def validate_publication_time(self) -> BacktestTargetAdjustmentView:
        if self.published_at > self.effective_at:
            raise ValueError("adjustment must be published before it becomes effective")
        return self


class BacktestOrderView(StrictContract):
    id: UUID
    item_id: UUID
    signal_date: date
    execute_date: date | None
    status: BacktestOrderStatus
    direction: BacktestOrderDirection
    execution_price: Decimal | None = Field(default=None, gt=0)
    quantity: Decimal | None = Field(default=None, gt=0)
    cash_before: Decimal = Field(ge=0)
    position_before: Decimal = Field(ge=0)
    target_values: TargetValues
    target_zone: SignalZone

    @model_validator(mode="after")
    def validate_execution(self) -> BacktestOrderView:
        is_filled = self.status is BacktestOrderStatus.FILLED
        execution_fields = (self.execute_date, self.execution_price, self.quantity)
        has_execution = all(value is not None for value in execution_fields)
        if is_filled != has_execution:
            raise ValueError(
                "filled status must match execution date, price, and quantity"
            )
        if not is_filled and any(value is not None for value in execution_fields):
            raise ValueError(
                "pending or unfilled order cannot contain execution values"
            )
        if self.execute_date is not None and self.execute_date <= self.signal_date:
            raise ValueError("order execution must occur on D+1 or later")
        return self


class BacktestTradeView(StrictContract):
    id: UUID
    item_id: UUID
    order_id: UUID
    execute_date: date
    direction: BacktestOrderDirection
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    cash_after: Decimal = Field(ge=0)
    position_after: Decimal = Field(ge=0)
    target_values: TargetValues
    target_zone: SignalZone
    round_trip_no: int = Field(ge=1)
    holding_trade_days: int | None = Field(default=None, ge=0)
    realized_return_amount: Decimal | None = None
    realized_return_rate: Decimal | None = None

    @model_validator(mode="after")
    def validate_realized_return(self) -> BacktestTradeView:
        realized_values = (
            self.holding_trade_days,
            self.realized_return_amount,
            self.realized_return_rate,
        )
        if self.direction is BacktestOrderDirection.SELL:
            if any(value is None for value in realized_values):
                raise ValueError("sell trade requires holding days and realized return")
        elif any(value is not None for value in realized_values):
            raise ValueError("buy trade cannot include realized return")
        return self


class BacktestMetricView(StrictContract):
    item_id: UUID
    ending_equity: Decimal = Field(ge=0)
    total_return: Decimal
    realized_return: Decimal
    annualized_return: Decimal
    max_drawdown: Decimal
    volatility: Decimal = Field(ge=0)
    sharpe_ratio: Decimal | None
    completed_round_trips: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    breakeven_trades: int = Field(ge=0)
    win_rate: Decimal | None = Field(default=None, ge=0, le=1)
    average_trade_return: Decimal | None
    maximum_trade_gain: Decimal | None
    maximum_trade_loss: Decimal | None
    average_holding_trade_days: Decimal | None = Field(default=None, ge=0)
    longest_holding_trade_days: int = Field(ge=0)
    capital_exposure_ratio: Decimal = Field(ge=0, le=1)
    open_position_at_end: bool
    unfilled_order_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_trade_counts(self) -> BacktestMetricView:
        if (
            self.winning_trades + self.losing_trades + self.breakeven_trades
            != self.completed_round_trips
        ):
            raise ValueError(
                "winning, losing, and breakeven trades must equal completed round trips"
            )
        if self.completed_round_trips == 0 and self.win_rate is not None:
            raise ValueError("win rate is unavailable when there are no trades")
        if self.completed_round_trips > 0 and self.win_rate is None:
            raise ValueError("win rate is required when trades exist")
        return self


class BacktestDailyResultView(StrictContract):
    item_id: UUID
    trade_date: date
    cash: Decimal = Field(ge=0)
    position_quantity: Decimal = Field(ge=0)
    close_price: Decimal = Field(gt=0)
    position_market_value: Decimal = Field(ge=0)
    equity: Decimal = Field(ge=0)
    drawdown: Decimal = Field(ge=0, le=1)
    target_values: TargetValues
    zone: SignalZone
    position_status: BacktestPositionStatus

    @model_validator(mode="after")
    def validate_equity(self) -> BacktestDailyResultView:
        if self.cash + self.position_market_value != self.equity:
            raise ValueError("equity must equal cash plus position market value")
        return self


class BacktestResultView(StrictContract):
    task_id: UUID
    item_id: UUID
    item_status: BacktestItemStatus
    forecast: BacktestForecastSnapshotView | None
    test_data_snapshot: BacktestTestDataSnapshotView | None = None
    adjustment_snapshot: AdjustmentTimelineSnapshot | None = None
    adjustments: tuple[BacktestTargetAdjustmentView, ...]
    orders: tuple[BacktestOrderView, ...]
    trades: tuple[BacktestTradeView, ...]
    daily_results: tuple[BacktestDailyResultView, ...]
    metric: BacktestMetricView | None


class BacktestItemSummaryView(StrictContract):
    item_id: UUID
    security_id: UUID
    symbol: str
    name: str
    status: BacktestItemStatus
    failure_code: str | None = None
    attempt_count: int = Field(ge=0)
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None


class BacktestTaskListItemView(StrictContract):
    task_id: UUID
    rerun_from_task_id: UUID | None = None
    mode: BacktestMode
    status: BacktestTaskStatus
    date_range: BacktestDateRange
    item: BacktestItemSummaryView
    allowed_actions: tuple[BacktestAction, ...]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    terminal_at: AwareDatetime | None = None


class BacktestTaskPage(StrictContract):
    items: tuple[BacktestTaskListItemView, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    total: int = Field(ge=0)


class BacktestSummaryView(StrictContract):
    task_id: UUID
    status: BacktestTaskStatus
    total_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    succeeded_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    canceled_items: int = Field(ge=0)
    pending_items: int = Field(ge=0)
    failure_codes: Mapping[str, int]
    allowed_actions: tuple[BacktestAction, ...]
    metric: BacktestMetricView | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> BacktestSummaryView:
        if self.completed_items > self.total_items:
            raise ValueError("completed item count cannot exceed total")
        if (
            self.succeeded_items + self.failed_items + self.canceled_items
            != self.completed_items
        ):
            raise ValueError("terminal item counts must equal completed count")
        if self.completed_items + self.pending_items != self.total_items:
            raise ValueError("completed and pending counts must equal total")
        return self
