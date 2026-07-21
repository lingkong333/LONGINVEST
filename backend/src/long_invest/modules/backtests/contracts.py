from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
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

from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.targets.contracts import TargetValues


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_deep_thaw(item) for item in value]
    return value


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


class BacktestTaskSnapshot(StrictContract):
    id: UUID
    mode: BacktestMode
    universe_snapshot: tuple[BacktestUniverseEntry, ...]
    universe_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    date_range: BacktestDateRange
    strategy_version_id: UUID | None
    draft_source_code: str | None
    source_code_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_snapshot: Mapping[str, Any]
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_version: str = Field(min_length=1)
    runner_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    strategy_api_version: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    hysteresis_ratio: Decimal = Field(ge=0)
    minimum_hysteresis: Decimal = Field(ge=0)
    initial_capital: Decimal = Field(gt=0)
    price_basis: str = Field(min_length=1)
    data_source: str = Field(min_length=1)

    @field_validator("parameter_snapshot")
    @classmethod
    def freeze_parameters(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)

    @field_serializer("parameter_snapshot")
    def serialize_parameters(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _deep_thaw(value)

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
        has_draft = self.draft_source_code is not None
        if has_version == has_draft:
            raise ValueError("choose one published version or frozen draft source")
        if self.draft_source_code is not None and not self.draft_source_code.strip():
            raise ValueError("draft source code must not be blank")
        return self


class BacktestForecastSnapshotView(StrictContract):
    item_id: UUID
    training_start_date: date
    training_end_date: date
    training_row_count: int = Field(gt=0)
    training_fetched_at: AwareDatetime
    training_data_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_code_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: TargetValues
    diagnostics: Mapping[str, Any]
    environment_version: str = Field(min_length=1)
    runner_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    price_basis: str = Field(min_length=1)
    frozen_at: AwareDatetime

    @field_validator("diagnostics")
    @classmethod
    def freeze_diagnostics(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)

    @field_serializer("diagnostics")
    def serialize_diagnostics(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _deep_thaw(value)

    @model_validator(mode="after")
    def validate_training_snapshot(self) -> BacktestForecastSnapshotView:
        if self.training_start_date > self.training_end_date:
            raise ValueError("training range is invalid")
        if self.training_fetched_at > self.frozen_at:
            raise ValueError("training data must be fetched before forecast is frozen")
        return self


class BacktestTargetAdjustmentView(StrictContract):
    item_id: UUID
    event_date: date
    before_values: TargetValues
    after_values: TargetValues
    adjustment_factor: Decimal = Field(gt=0)
    source: str = Field(min_length=1)
    data_hash: str = Field(min_length=64, max_length=64)
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
    quantity: Decimal = Field(gt=0)
    cash_before: Decimal = Field(ge=0)
    position_before: Decimal = Field(ge=0)
    target_values: TargetValues
    target_zone: SignalZone

    @model_validator(mode="after")
    def validate_execution(self) -> BacktestOrderView:
        is_filled = self.status is BacktestOrderStatus.FILLED
        has_execution = (
            self.execute_date is not None and self.execution_price is not None
        )
        if is_filled != has_execution:
            raise ValueError("filled status must match execution date and price")
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
        if self.winning_trades + self.losing_trades != self.completed_round_trips:
            raise ValueError(
                "winning and losing trades must equal completed round trips"
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
