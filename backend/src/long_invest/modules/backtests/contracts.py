from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class BacktestSignalRulePort(Protocol):
    def evaluate(self, signal: BacktestSignalInput) -> str: ...
