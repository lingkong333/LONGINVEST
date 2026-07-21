from __future__ import annotations

from collections.abc import Mapping
from datetime import date
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
)

from long_invest.modules.targets.contracts import TargetValues


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
        return MappingProxyType({str(key): item for key, item in value.items()})

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
        return tuple(MappingProxyType(dict(row)) for row in value)


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
        return MappingProxyType(dict(value))


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
