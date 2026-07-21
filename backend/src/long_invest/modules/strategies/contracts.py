# ruff: noqa: E501
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


class StrategyForecastErrorCode(StrEnum):
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    TRAINING_DATA_INVALID = "TRAINING_DATA_INVALID"
    STRATEGY_FORECAST_TIMEOUT = "STRATEGY_FORECAST_TIMEOUT"
    STRATEGY_TARGET_INVALID = "STRATEGY_TARGET_INVALID"
    TEST_DATA_EXPOSED_TO_STRATEGY = "TEST_DATA_EXPOSED_TO_STRATEGY"


class StrategyReadinessStatus(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    ARCHIVED = "ARCHIVED"


class FrozenMappingContract(StrictContract):
    parameter_snapshot: Mapping[str, Any]

    @field_validator("parameter_snapshot")
    @classmethod
    def freeze_parameter_snapshot(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)

    @field_serializer("parameter_snapshot")
    def serialize_parameter_snapshot(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class TrainingDataSnapshot(StrictContract):
    security_id: UUID
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    start_date: date
    end_date: date
    data_version: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)
    rows: tuple[Mapping[str, Any], ...]

    @field_validator("rows")
    @classmethod
    def freeze_rows(
        cls, value: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(_deep_freeze(row) for row in value)

    @model_validator(mode="after")
    def validate_rows(self) -> TrainingDataSnapshot:
        if not self.rows:
            raise ValueError("training rows must not be empty")
        dates = [row.get("trade_date") for row in self.rows]
        if any(not isinstance(value, date) for value in dates):
            raise ValueError("training rows require trade_date")
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ValueError("training rows must be strictly ordered")
        if dates[0] < self.start_date or dates[-1] > self.end_date:
            raise ValueError("training rows exceed requested range")
        for row in self.rows:
            try:
                low, open_, close, high = (
                    Decimal(str(row[key])) for key in ("low", "open", "close", "high")
                )
            except (KeyError, ValueError) as exc:
                raise ValueError("training rows require OHLC") from exc
            if low > min(open_, close) or high < max(open_, close):
                raise ValueError("training row OHLC is inconsistent")
        return self


class StrategyForecastRequest(FrozenMappingContract):
    strategy_version_id: UUID
    source_code_hash: str = Field(min_length=64, max_length=64)
    parameter_hash: str = Field(min_length=64, max_length=64)
    training_data: TrainingDataSnapshot
    requested_at: AwareDatetime


class StrategyForecastResult(StrictContract):
    values: TargetValues
    diagnostics: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("diagnostics")
    @classmethod
    def freeze_diagnostics(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)


class StrategyReadiness(StrictContract):
    strategy_version_id: UUID
    status: StrategyReadinessStatus
    checked_at: AwareDatetime
    failure_code: StrategyForecastErrorCode | None = None


class StrategyForecastPort(Protocol):
    async def forecast(
        self, request: StrategyForecastRequest
    ) -> StrategyForecastResult: ...


class TrainingDataPort(Protocol):
    async def get_training_data(
        self,
        *,
        security_id: UUID,
        start_date: date,
        end_date: date,
    ) -> TrainingDataSnapshot | None: ...


class StrategyReadinessPort(Protocol):
    async def get_strategy_readiness(
        self, strategy_version_id: UUID
    ) -> StrategyReadiness | None: ...
