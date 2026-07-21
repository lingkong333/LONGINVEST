# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, DecimalException
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


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_deep_thaw(item) for item in value]
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


class StrategyLifecycleStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    ARCHIVED = "ARCHIVED"


class StrategyLifecycleErrorCode(StrEnum):
    STRATEGY_VERSION_CONFLICT = "STRATEGY_VERSION_CONFLICT"
    STRATEGY_NOT_READY = "STRATEGY_NOT_READY"
    STRATEGY_VALIDATION_REQUIRED = "STRATEGY_VALIDATION_REQUIRED"
    STRATEGY_VALIDATION_STALE = "STRATEGY_VALIDATION_STALE"
    STRATEGY_PUBLISH_IN_PROGRESS = "STRATEGY_PUBLISH_IN_PROGRESS"
    STRATEGY_PUBLISH_FAILED = "STRATEGY_PUBLISH_FAILED"
    STRATEGY_VERSION_IMMUTABLE = "STRATEGY_VERSION_IMMUTABLE"
    STRATEGY_ARCHIVED = "STRATEGY_ARCHIVED"


class FrozenMappingContract(StrictContract):
    parameter_snapshot: Mapping[str, Any]

    @field_validator("parameter_snapshot")
    @classmethod
    def freeze_parameter_snapshot(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)

    @field_serializer("parameter_snapshot")
    def serialize_parameter_snapshot(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _deep_thaw(value)


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

    @field_serializer("rows")
    def serialize_rows(self, value: tuple[Mapping[str, Any], ...]) -> list[Any]:
        return _deep_thaw(value)

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
            except (KeyError, ValueError, DecimalException) as exc:
                raise ValueError("training rows require OHLC") from exc
            if (
                any(
                    not value.is_finite() or value <= 0
                    for value in (low, open_, close, high)
                )
                or low > min(open_, close)
                or high < max(open_, close, low)
            ):
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

    @field_serializer("diagnostics")
    def serialize_diagnostics(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _deep_thaw(value)


class StrategyReadiness(StrictContract):
    strategy_version_id: UUID
    status: StrategyReadinessStatus
    checked_at: AwareDatetime
    failure_code: StrategyForecastErrorCode | None = None


class StrategyView(StrictContract):
    id: UUID
    name: str = Field(min_length=1)
    status: StrategyLifecycleStatus


class StrategyDraftView(StrictContract):
    id: UUID
    strategy_id: UUID
    draft_version: int = Field(ge=1)
    source_code: str


class StrategyDraftRevisionView(StrategyDraftView):
    revision_no: int = Field(ge=1)


class StrategyVersionView(StrictContract):
    id: UUID
    strategy_id: UUID
    version_no: int = Field(ge=1)
    source_code: str = Field(min_length=1)
    metadata: Mapping[str, Any]
    parameter_schema: Mapping[str, Any]
    environment_version: str = Field(min_length=1)
    runner_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_code_hash: str = Field(min_length=64, max_length=64)
    git_commit: str = Field(min_length=7, max_length=64)
    validation_run_id: UUID
    status: StrategyLifecycleStatus
    published_at: AwareDatetime
    created_at: AwareDatetime

    @field_validator("metadata", "parameter_schema")
    @classmethod
    def freeze_release_mapping(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)

    @field_serializer("metadata", "parameter_schema")
    def serialize_release_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _deep_thaw(value)


class StrategyValidationRunView(StrictContract):
    id: UUID
    status: StrategyLifecycleStatus
    error_code: StrategyLifecycleErrorCode | None = None


class StrategyRunView(StrictContract):
    id: UUID
    strategy_version_id: UUID
    status: StrategyLifecycleStatus


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
