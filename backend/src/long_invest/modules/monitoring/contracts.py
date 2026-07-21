from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubscriptionStatus(StrEnum):
    CONFIGURING = "CONFIGURING"
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class OccurrenceStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DISPATCHED = "DISPATCHED"
    MISSED = "MISSED"
    FAILED = "FAILED"


class TargetReadinessPort(Protocol):
    async def current_readiness(self, subscription_id: UUID) -> bool: ...


class StrategyReadinessPort(Protocol):
    async def published_version(self, strategy_version_id: UUID) -> bool: ...


class FrozenSubscription(StrictContract):
    subscription_id: UUID
    security_id: UUID
    symbol: str
    version: int = Field(ge=1)
    revision_id: UUID


class SubscriptionSignalSnapshot(StrictContract):
    subscription_id: UUID
    security_id: UUID
    symbol: str
    status: SubscriptionStatus
    version: int = Field(ge=1)
    revision_id: UUID
    target_mode: str
    strategy_version_id: UUID | None = None
    parameter_snapshot: Mapping[str, Any] = Field(default_factory=dict)
    hysteresis_ratio: Decimal = Field(ge=0)
    hysteresis_min: Decimal = Field(ge=0)
    notification_mode: str

    @field_validator("parameter_snapshot", mode="after")
    @classmethod
    def freeze_signal_parameters(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)

    @field_serializer("parameter_snapshot")
    def serialize_signal_parameters(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _deep_thaw(value)


class FrozenScheduleSubscriptions(StrictContract):
    schedule_id: UUID
    subscriptions: tuple[FrozenSubscription, ...]


class MonitorSubscriptionView(StrictContract):
    id: UUID
    security_id: UUID
    symbol: str
    status: SubscriptionStatus
    version: int = Field(ge=1)
    current_revision_id: UUID | None
    archived_at: datetime | None


class MonitorSubscriptionRevisionView(StrictContract):
    id: UUID
    subscription_id: UUID
    revision_no: int = Field(ge=1)
    schedule_id: UUID | None
    schedule_revision_id: UUID | None
    target_mode: str
    target_version_id: UUID | None
    strategy_version_id: UUID | None
    parameter_snapshot: Mapping[str, Any]
    hysteresis_ratio: Decimal = Field(ge=0)
    hysteresis_min: Decimal = Field(ge=0)
    notification_mode: str = Field(min_length=1, max_length=64)

    @field_validator("parameter_snapshot", mode="after")
    @classmethod
    def freeze_parameters(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)

    @field_serializer("parameter_snapshot")
    def serialize_parameters(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _deep_thaw(value)


class ScheduleOccurrenceView(StrictContract):
    id: UUID
    occurrence_type: str
    schedule_id: UUID
    scheduled_at: datetime
    status: OccurrenceStatus
    subscriptions: tuple[FrozenSubscription, ...]
    job_id: UUID | None = None
    error_code: str | None = None


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
