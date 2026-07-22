from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from long_invest.modules.monitoring.contracts import (
    SubscriptionNotificationChannel,
    SubscriptionNotificationMode,
    SubscriptionSignalSnapshot,
)
from long_invest.modules.positions.contracts import PositionSnapshot, PositionStatus
from long_invest.modules.quotes.contracts import SignalQuoteSnapshot
from long_invest.modules.targets.contracts import TargetSnapshot, TargetValues


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


MAX_SIGNAL_PRICE = Decimal("100000000000000")


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


class PriceZoneRuleInput(StrictContract):
    price: Decimal
    targets: TargetValues
    previous_zone: SignalZone
    hysteresis_ratio: Decimal = Field(ge=0)
    hysteresis_min: Decimal = Field(ge=0)

    @field_validator("price")
    @classmethod
    def validate_rule_price(cls, value: Decimal) -> Decimal:
        return _validate_signal_price(value)

    @field_validator("hysteresis_ratio", "hysteresis_min")
    @classmethod
    def validate_rule_hysteresis(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("hysteresis must be finite")
        return value


class PriceZoneRuleResult(StrictContract):
    zone: SignalZone


class PriceZoneRulePort(Protocol):
    def evaluate(self, value: PriceZoneRuleInput) -> PriceZoneRuleResult: ...


class SignalInput(StrictContract):
    subscription_id: UUID
    security_id: UUID
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    security_name: str = Field(min_length=1, max_length=100)
    subscription_version: int = Field(ge=1)
    price: Decimal
    price_at: AwareDatetime
    quote_scheduled_at: AwareDatetime | None = None
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
    request_id: str = Field(min_length=1, max_length=64)
    quote_eligible: bool = True
    quote_ineligibility_code: str | None = Field(default=None, max_length=100)

    @field_validator("price")
    @classmethod
    def validate_price(cls, value: Decimal) -> Decimal:
        return _validate_signal_price(value)

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

    @model_validator(mode="after")
    def validate_quote_eligibility(self) -> SignalInput:
        if not self.quote_eligible and not self.quote_ineligibility_code:
            raise ValueError("ineligible quote requires a reason")
        if (self.quote_cycle_id is None) != (self.quote_scheduled_at is None):
            raise ValueError("quote cycle and scheduled time must be provided together")
        if (self.quote_cycle_id is None) != (self.quote_item_id is None):
            raise ValueError("quote cycle and item must be provided together")
        return self


class SignalStateView(StrictContract):
    subscription_id: UUID
    zone: SignalZone
    version: int = Field(ge=1)
    last_price: Decimal | None = None
    last_price_at: AwareDatetime | None = None
    last_subscription_version: int | None = Field(default=None, ge=1)
    last_price_version: int | None = Field(default=None, ge=1)
    last_quote_cycle_id: UUID | None = None
    last_quote_scheduled_at: AwareDatetime | None = None
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
    quote_scheduled_at: AwareDatetime | None = None
    quote_item_id: UUID | None = None
    hysteresis_applied: bool
    used_stale_target: bool
    skip_code: str | None = None
    content_hash: str = Field(min_length=64, max_length=64)
    created_at: AwareDatetime

    @field_validator("price")
    @classmethod
    def validate_price_capacity(cls, value: Decimal | None) -> Decimal | None:
        return _validate_signal_price(value) if value is not None else None

    @model_validator(mode="after")
    def validate_non_skipped_inputs(self) -> SignalEvaluationView:
        if self.result not in {
            EvaluationResult.APPLIED,
            EvaluationResult.UNCHANGED,
        }:
            return self
        required = (
            self.subscription_version,
            self.target_revision_id,
            self.target_version,
            self.target_date,
            self.targets,
            self.position_version,
            self.position_status,
            self.price,
            self.price_at,
            self.price_version,
        )
        if any(value is None for value in required):
            raise ValueError(
                "non-skipped evaluation requires a complete input snapshot"
            )
        return self


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
    quote_scheduled_at: AwareDatetime | None = None
    quote_item_id: UUID | None = None
    used_stale_target: bool
    state_version: int = Field(ge=1)
    notification_class: NotificationClass
    notification_eligible: bool
    suppression_reason: str | None = None
    created_at: AwareDatetime

    @field_validator("price")
    @classmethod
    def validate_price_capacity(cls, value: Decimal) -> Decimal:
        return _validate_signal_price(value)


class EvaluationOutcome(StrictContract):
    code: str = Field(min_length=1, max_length=100)
    result: EvaluationResult
    state: SignalStateView | None = None
    evaluation: SignalEvaluationView
    event: SignalEventView | None = None
    replayed: bool = False


class SignalNotificationRequest(StrictContract):
    event_id: UUID
    subscription_id: UUID
    security_id: UUID
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    security_name: str = Field(min_length=1, max_length=100)
    notification_class: NotificationClass
    before_zone: SignalZone
    after_zone: SignalZone
    price: Decimal
    price_at: AwareDatetime
    targets: TargetValues
    target_revision_id: UUID
    target_version: int = Field(ge=1)
    target_date: date
    target_stale: bool
    position_status: PositionStatus
    position_version: int = Field(ge=0)
    reason: EvaluationReason
    notification_mode: SubscriptionNotificationMode
    notification_channels: tuple[SubscriptionNotificationChannel, ...] = ()
    eligible: bool
    suppression_reason: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)

    @field_validator("price")
    @classmethod
    def validate_notification_price(cls, value: Decimal) -> Decimal:
        return _validate_signal_price(value)


class SignalActionCommand(StrictContract):
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


class SignalStateResetCommand(SignalActionCommand):
    pass


class SignalReevaluationCommand(SignalActionCommand):
    pass


class SignalStateMutationResult(StrictContract):
    code: str
    subscription_id: UUID
    state: SignalStateView
    reevaluation_job_id: UUID
    replayed: bool = False


class SignalReevaluationResult(StrictContract):
    code: str
    subscription_id: UUID
    reevaluation_job_id: UUID
    accepted: bool
    replayed: bool = False


class SubscriptionSnapshotPort(Protocol):
    async def get_subscription_snapshot(
        self, subscription_id: UUID
    ) -> SubscriptionSignalSnapshot | None: ...


class PositionSnapshotPort(Protocol):
    async def get_position_snapshot(
        self, security_id: UUID
    ) -> PositionSnapshot | None: ...


class TargetSnapshotPort(Protocol):
    async def get_target_snapshot(
        self, subscription_id: UUID
    ) -> TargetSnapshot | None: ...


class QuoteSnapshotPort(Protocol):
    async def get_quote_snapshot(
        self,
        *,
        item_id: UUID,
        cycle_id: UUID,
    ) -> SignalQuoteSnapshot | None: ...


class SignalNotificationPort(Protocol):
    async def publish(self, notification: SignalNotificationRequest) -> object: ...


def _validate_signal_price(value: Decimal) -> Decimal:
    try:
        if not value.is_finite():
            raise ValueError("price must be finite")
        normalized = value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError("price cannot be represented at 0.000001") from exc
    if not normalized.is_finite() or normalized <= 0 or normalized >= MAX_SIGNAL_PRICE:
        raise ValueError("price is outside Numeric(20,6) capacity")
    return normalized
