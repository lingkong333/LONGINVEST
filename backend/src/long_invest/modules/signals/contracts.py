from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from long_invest.modules.monitoring.contracts import FrozenSubscription
from long_invest.modules.positions.contracts import PositionStatus, PositionView
from long_invest.modules.targets.contracts import TargetSnapshot, TargetValues


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SignalZone(StrEnum):
    UNKNOWN = "UNKNOWN"
    STRONG_LOW = "STRONG_LOW"
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    STRONG_HIGH = "STRONG_HIGH"


class EvaluationReason(StrEnum):
    SCHEDULED_QUOTE = "SCHEDULED_QUOTE"
    MANUAL_CHECK = "MANUAL_CHECK"
    TARGET_ACTIVATED = "TARGET_ACTIVATED"
    POSITION_BECAME_HOLDING = "POSITION_BECAME_HOLDING"
    DATA_CORRECTION = "DATA_CORRECTION"
    STATE_RESET = "STATE_RESET"
    RECOVERY_REEVALUATION = "RECOVERY_REEVALUATION"


class EvaluationResult(StrEnum):
    APPLIED = "APPLIED"
    UNCHANGED = "UNCHANGED"
    SKIPPED = "SKIPPED"
    SUPERSEDED = "SUPERSEDED"


class NotificationClass(StrEnum):
    LOW = "LOW"
    LOW_CLEARED = "LOW_CLEARED"
    HIGH = "HIGH"
    HIGH_CLEARED = "HIGH_CLEARED"


class SignalInput(StrictContract):
    subscription_id: UUID
    security_id: UUID
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    subscription_version: int = Field(ge=1)
    price: Decimal
    price_at: AwareDatetime
    price_version: int = Field(ge=1)
    target_revision_id: UUID
    target_version: int = Field(ge=1)
    target_date: date
    targets: TargetValues
    quote_cycle_id: UUID | None = None
    quote_item_id: UUID | None = None
    position_version: int = Field(ge=0)
    hysteresis_ratio: Decimal = Field(ge=0)
    hysteresis_min: Decimal = Field(ge=0)
    reason: EvaluationReason
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("price must be positive and finite")
        return value

    @field_validator("hysteresis_ratio", "hysteresis_min")
    @classmethod
    def validate_finite_hysteresis(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("hysteresis must be finite")
        return value

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def strip_idempotency_key(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SignalStateView(StrictContract):
    subscription_id: UUID
    zone: SignalZone
    version: int = Field(ge=1)
    last_price: Decimal | None = None
    last_price_at: AwareDatetime | None = None
    last_subscription_version: int | None = Field(default=None, ge=1)
    last_price_version: int | None = Field(default=None, ge=1)
    last_quote_cycle_id: UUID | None = None
    last_quote_item_id: UUID | None = None
    last_target_revision_id: UUID | None = None
    last_target_version: int | None = Field(default=None, ge=1)
    last_position_version: int | None = Field(default=None, ge=0)


class SignalEvaluationView(StrictContract):
    id: UUID
    subscription_id: UUID
    reason: EvaluationReason
    result: EvaluationResult
    before_zone: SignalZone
    after_zone: SignalZone
    subscription_version: int | None = Field(default=None, ge=1)
    target_revision_id: UUID | None = None
    target_version: int | None = Field(default=None, ge=1)
    target_date: date | None = None
    targets: TargetValues | None = None
    position_status: PositionStatus | None = None
    position_version: int | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, gt=0)
    price_at: AwareDatetime | None = None
    price_version: int | None = Field(default=None, ge=1)
    quote_cycle_id: UUID | None = None
    quote_item_id: UUID | None = None
    hysteresis_applied: bool
    used_stale_target: bool
    skip_code: str | None = None
    content_hash: str = Field(min_length=64, max_length=64)
    created_at: AwareDatetime


class SignalEventView(StrictContract):
    id: UUID
    subscription_id: UUID
    evaluation_id: UUID
    before_zone: SignalZone
    after_zone: SignalZone
    reason: EvaluationReason
    price: Decimal = Field(gt=0)
    price_at: AwareDatetime
    targets: TargetValues
    target_revision_id: UUID
    target_version: int = Field(ge=1)
    target_date: date
    position_status: PositionStatus
    position_version: int = Field(ge=0)
    quote_cycle_id: UUID | None = None
    quote_item_id: UUID | None = None
    used_stale_target: bool
    state_version: int = Field(ge=1)
    notification_class: NotificationClass
    notification_eligible: bool
    suppression_reason: str | None = None
    created_at: AwareDatetime


class EvaluationOutcome(StrictContract):
    code: str = Field(min_length=1, max_length=100)
    result: EvaluationResult
    state: SignalStateView | None = None
    evaluation: SignalEvaluationView
    event: SignalEventView | None = None
    replayed: bool = False


class SubscriptionSnapshotPort(Protocol):
    async def get_subscription_snapshot(
        self, subscription_id: UUID
    ) -> FrozenSubscription | None: ...


class PositionSnapshotPort(Protocol):
    async def get_position_snapshot(
        self, security_id: UUID
    ) -> PositionView | None: ...


class TargetSnapshotPort(Protocol):
    async def get_target_snapshot(
        self, subscription_id: UUID
    ) -> TargetSnapshot | None: ...
