from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PositionStatus(StrEnum):
    HOLDING = "HOLDING"
    NOT_HOLDING = "NOT_HOLDING"


class SetPosition(StrictContract):
    security_id: UUID
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    target: PositionStatus
    note: str | None = Field(default=None, max_length=500)
    source: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_user_id: str = Field(min_length=1, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator(
        "note",
        "source",
        "request_id",
        "idempotency_key",
        "actor_user_id",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class PositionView(StrictContract):
    security_id: UUID
    symbol: str
    status: PositionStatus
    version: int = Field(ge=0)
    source: str | None = None
    updated_at: datetime | None = None


class PositionHistoryView(StrictContract):
    id: UUID
    security_id: UUID
    before_status: PositionStatus | None
    after_status: PositionStatus
    version: int = Field(ge=1)
    note: str | None
    effective_at: datetime | None = None
