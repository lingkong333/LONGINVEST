from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.targets.contracts import TargetValues


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class BacktestSignalRuleInput(BacktestSignalInput):
    previous_zone: SignalZone
    position_status: BacktestPositionStatus
    hysteresis_ratio: Decimal = Field(ge=0)
    minimum_hysteresis: Decimal = Field(ge=0)


class BacktestSignalRuleResult(StrictContract):
    zone: SignalZone


class BacktestSignalRulePort(Protocol):
    def evaluate(self, signal: BacktestSignalRuleInput) -> BacktestSignalRuleResult: ...


class BacktestTaskSnapshot(StrictContract):
    id: UUID
    date_range: BacktestDateRange
    source_code_hash: str = Field(min_length=64, max_length=64)
    parameter_hash: str = Field(min_length=64, max_length=64)
    environment_version: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    initial_capital: Decimal = Field(gt=0)
    price_basis: str = Field(min_length=1)
    data_source: str = Field(min_length=1)


class BacktestForecastSnapshotView(StrictContract):
    item_id: UUID
    values: TargetValues
    training_data_hash: str = Field(min_length=64, max_length=64)
    frozen_at: AwareDatetime


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


class BacktestOrderView(StrictContract):
    id: UUID
    item_id: UUID
    signal_date: date
    execute_date: date
    status: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)


class BacktestTradeView(StrictContract):
    id: UUID
    order_id: UUID
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    cash_after: Decimal
    position_after: Decimal = Field(ge=0)


class BacktestMetricView(StrictContract):
    item_id: UUID
    total_return: Decimal
    max_drawdown: Decimal
    completed_round_trips: int = Field(ge=0)


class BacktestDailyResultView(StrictContract):
    item_id: UUID
    trade_date: date
    equity: Decimal = Field(ge=0)
    drawdown: Decimal
