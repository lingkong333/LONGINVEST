from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
            if not value.is_finite() or value <= 0:
                raise ValueError("target prices must be positive and finite")
            return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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
    values: TargetValues
    large_change_confirmed: bool = False
    switch_to_manual_confirmed: bool = False


class RestoreTargetCommand(TargetCommand):
    source_revision_id: UUID
    switch_to_manual_confirmed: bool = False


class TargetRevisionView(StrictContract):
    id: UUID
    subscription_id: UUID
    revision_no: int = Field(ge=1)
    values: TargetValues
    source: TargetSource
    source_revision_id: UUID | None = None
    reason: str
    created_at: datetime


class TargetBindingView(StrictContract):
    subscription_id: UUID
    current_revision_id: UUID | None
    status: TargetStatus
    version: int = Field(ge=1)
    activated_at: datetime | None = None
    stale_reason: str | None = None


class TargetSnapshot(StrictContract):
    subscription_id: UUID
    revision_id: UUID
    revision_no: int = Field(ge=1)
    binding_version: int = Field(ge=1)
    values: TargetValues
    source: TargetSource
    status: TargetStatus
    activated_at: datetime


class TargetMutationResult(StrictContract):
    code: str = Field(min_length=1, max_length=100)
    binding: TargetBindingView
    revision: TargetRevisionView
    replayed: bool = False


class TargetSnapshotPort(Protocol):
    async def get_target_snapshot(
        self, subscription_id: UUID
    ) -> TargetSnapshot | None: ...
