from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, DecimalException
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


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TargetSource(StrEnum):
    MANUAL = "MANUAL"
    RESTORED = "RESTORED"


class TargetStatus(StrEnum):
    READY = "READY"
    STALE = "STALE"
    CALCULATING = "CALCULATING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    ACTIVATING = "ACTIVATING"
    FAILED = "FAILED"
    MISSING = "MISSING"


class TargetValues(StrictContract):
    low_strong: Decimal
    low_watch: Decimal
    high_watch: Decimal
    high_strong: Decimal

    @field_validator("low_strong", "low_watch", "high_watch", "high_strong")
    @classmethod
    def quantize_price(cls, value: Decimal) -> Decimal:
        try:
            if not value.is_finite():
                raise ValueError("target prices must be positive and finite")
            quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if not quantized.is_finite() or quantized <= 0:
                raise ValueError("target prices must be positive after quantization")
            return quantized
        except DecimalException as exc:
            raise ValueError("target price cannot be represented at 0.01") from exc

    @model_validator(mode="after")
    def validate_order(self) -> TargetValues:
        if not (
            self.low_strong
            < self.low_watch
            < self.high_watch
            < self.high_strong
        ):
            raise ValueError("target prices must be strictly increasing")
        return self


class TargetCommand(StrictContract):
    subscription_id: UUID
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=64)
    actor_user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    trusted_ip: str = Field(min_length=1, max_length=64)

    @field_validator(
        "reason",
        "idempotency_key",
        "request_id",
        "actor_user_id",
        "session_id",
        "trusted_ip",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ManualTargetCommand(TargetCommand):
    target_date: date
    values: TargetValues
    large_change_confirmed: bool = False
    switch_to_manual_confirmed: bool = False


class RestoreTargetCommand(TargetCommand):
    source_revision_id: UUID
    switch_to_manual_confirmed: bool = False


class FrozenParametersContract(StrictContract):
    parameter_snapshot: Mapping[str, Any]

    @field_validator("parameter_snapshot", mode="after")
    @classmethod
    def freeze_parameters(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _deep_freeze(value)

    @field_serializer("parameter_snapshot")
    def serialize_parameters(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _deep_thaw(value)


class TargetRevisionView(FrozenParametersContract):
    id: UUID
    subscription_id: UUID
    revision_no: int = Field(ge=1)
    values: TargetValues
    source: TargetSource
    source_revision_id: UUID | None = None
    target_date: date
    strategy_version_id: UUID | None = None
    data_version: int | None = Field(default=None, ge=1)
    source_code_hash: str | None = Field(default=None, min_length=64, max_length=64)
    content_hash: str = Field(min_length=64, max_length=64)
    reason: str
    created_at: AwareDatetime


class TargetBindingView(StrictContract):
    subscription_id: UUID
    current_revision_id: UUID | None
    status: TargetStatus
    version: int = Field(ge=1)
    activated_at: AwareDatetime | None = None
    stale_reason: str | None = None


class TargetSnapshot(FrozenParametersContract):
    subscription_id: UUID
    revision_id: UUID
    revision_no: int = Field(ge=1)
    binding_version: int = Field(ge=1)
    values: TargetValues
    source: TargetSource
    status: TargetStatus
    target_date: date
    strategy_version_id: UUID | None = None
    data_version: int | None = Field(default=None, ge=1)
    source_code_hash: str | None = Field(default=None, min_length=64, max_length=64)
    content_hash: str = Field(min_length=64, max_length=64)
    activated_at: AwareDatetime


class TargetMutationResult(StrictContract):
    code: str = Field(min_length=1, max_length=100)
    binding: TargetBindingView
    revision: TargetRevisionView
    replayed: bool = False


class TargetSnapshotPort(Protocol):
    async def get_target_snapshot(
        self, subscription_id: UUID
    ) -> TargetSnapshot | None: ...


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
