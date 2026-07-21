from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from enum import StrEnum
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

from long_invest.platform.json_snapshot import freeze_json_mapping, thaw_json_value
from long_invest.platform.validation import Sha256Hex


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


MAX_TARGET_PRICE = Decimal("1000000000000000000")


class TargetSource(StrEnum):
    MANUAL = "MANUAL"
    STRATEGY = "STRATEGY"
    RESTORED = "RESTORED"
    DATA_CORRECTION = "DATA_CORRECTION"
    STRATEGY_CHANGE = "STRATEGY_CHANGE"
    PARAMETER_CHANGE = "PARAMETER_CHANGE"


class TargetStatus(StrEnum):
    READY = "READY"
    STALE = "STALE"
    CALCULATING = "CALCULATING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    ACTIVATING = "ACTIVATING"
    FAILED = "FAILED"
    MISSING = "MISSING"


class TargetCalculationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TargetReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class TargetCalculationErrorCode(StrEnum):
    STRATEGY_FORECAST_TIMEOUT = "STRATEGY_FORECAST_TIMEOUT"
    STRATEGY_TARGET_INVALID = "STRATEGY_TARGET_INVALID"
    TARGET_CALCULATION_FAILED = "TARGET_CALCULATION_FAILED"


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
            if (
                not quantized.is_finite()
                or quantized <= 0
                or quantized >= MAX_TARGET_PRICE
            ):
                raise ValueError("target price is outside Numeric(20,2) capacity")
            return quantized
        except DecimalException as exc:
            raise ValueError("target price cannot be represented at 0.01") from exc

    @model_validator(mode="after")
    def validate_order(self) -> TargetValues:
        if not (self.low_strong < self.low_watch < self.high_watch < self.high_strong):
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
        return freeze_json_mapping(value)

    @field_serializer("parameter_snapshot")
    def serialize_parameters(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)


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
    source_code_hash: Sha256Hex | None = None
    content_hash: Sha256Hex
    reason: str
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_source_revision(self) -> TargetRevisionView:
        source_is_restored = self.source is TargetSource.RESTORED
        has_source_revision = self.source_revision_id is not None
        if source_is_restored != has_source_revision:
            raise ValueError("source revision must match target source")
        source_is_strategy = self.source is TargetSource.STRATEGY
        if source_is_strategy != (self.strategy_version_id is not None):
            raise ValueError("strategy version must match target source")
        return self


class TargetBindingView(StrictContract):
    subscription_id: UUID
    current_revision_id: UUID | None
    status: TargetStatus
    version: int = Field(ge=1)
    activated_at: AwareDatetime | None = None
    stale_reason: str | None = None


class TargetCalculationRunView(FrozenParametersContract):
    id: UUID
    subscription_id: UUID
    subscription_version: int = Field(ge=1)
    subscription_revision_id: UUID
    strategy_version_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_digest: Sha256Hex
    status: TargetCalculationStatus
    failure_code: TargetCalculationErrorCode | None = None
    training_start_date: date | None = None
    training_end_date: date | None = None
    qfq_data_version: int | None = Field(default=None, ge=1)
    current_target_version: int | None = Field(default=None, ge=1)
    reason: str | None = None
    resource_usage: Mapping[str, Any] = Field(default_factory=dict)
    error_summary: str | None = None
    created_at: AwareDatetime

    @field_validator("resource_usage")
    @classmethod
    def freeze_resource_usage(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json_mapping(value)

    @field_serializer("resource_usage")
    def serialize_resource_usage(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def validate_training_range(self) -> TargetCalculationRunView:
        has_start = self.training_start_date is not None
        has_end = self.training_end_date is not None
        if has_start != has_end:
            raise ValueError("training start and end dates must be provided together")
        if (
            self.training_start_date is not None
            and self.training_end_date is not None
            and self.training_start_date > self.training_end_date
        ):
            raise ValueError("training start date must not be after end date")
        return self


class TargetReviewView(StrictContract):
    id: UUID
    candidate_revision_id: UUID
    baseline_revision_id: UUID
    status: TargetReviewStatus
    reason: str = Field(min_length=1, max_length=500)
    low_strong_change: Decimal
    low_watch_change: Decimal
    high_watch_change: Decimal
    high_strong_change: Decimal
    reviewer_user_id: str | None = None
    review_comment: str | None = None
    reviewed_at: AwareDatetime | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_decision_metadata(self) -> TargetReviewView:
        decision_values = (
            self.reviewer_user_id,
            self.review_comment,
            self.reviewed_at,
        )
        is_decided = self.status in {
            TargetReviewStatus.APPROVED,
            TargetReviewStatus.REJECTED,
        }
        if is_decided and any(value is None for value in decision_values):
            raise ValueError("decided review requires reviewer, comment, and time")
        if not is_decided and any(value is not None for value in decision_values):
            raise ValueError("undecided review cannot include decision metadata")
        if self.reviewer_user_id is not None and not self.reviewer_user_id.strip():
            raise ValueError("reviewer must not be blank")
        if self.review_comment is not None and not self.review_comment.strip():
            raise ValueError("review comment must not be blank")
        return self


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
    source_code_hash: Sha256Hex | None = None
    content_hash: Sha256Hex
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
