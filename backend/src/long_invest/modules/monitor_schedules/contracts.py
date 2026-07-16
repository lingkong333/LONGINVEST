from __future__ import annotations

from datetime import datetime, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleDefinition(StrictContract):
    name: str = Field(min_length=1, max_length=100)
    times: tuple[time, ...] = ()
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("name", "reason", "idempotency_key", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("times")
    @classmethod
    def validate_times(cls, values: tuple[time, ...]) -> tuple[time, ...]:
        if len(values) > 20 or len(values) != len(set(values)):
            raise ValueError("times must be unique and contain at most 20 entries")
        for value in values:
            if (
                value.second
                or value.microsecond
                or not (
                    time(9, 30) <= value <= time(11, 30)
                    or time(13) <= value <= time(15)
                )
            ):
                raise ValueError("time is outside an A-share trading session")
        return tuple(sorted(values))


class ScheduleRevisionView(StrictContract):
    id: UUID
    schedule_id: UUID
    revision_no: int = Field(ge=1)
    times: tuple[time, ...]
    timezone: str = "Asia/Shanghai"
    reason: str
    created_at: datetime


class MonitorScheduleView(StrictContract):
    id: UUID
    name: str
    current_revision_id: UUID | None
    version: int = Field(ge=1)
    archived_at: datetime | None
