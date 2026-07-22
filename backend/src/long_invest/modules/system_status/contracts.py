from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class StatusDetail(StrictContract):
    key: str = Field(min_length=1, max_length=64)
    value: str | int | float | bool | None
    unit: str | None = Field(default=None, max_length=24)


class ComponentStatus(StrictContract):
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=32)
    status: HealthStatus
    critical: bool = False
    source: str = Field(min_length=1, max_length=64)
    updated_at: datetime
    message: str | None = Field(default=None, max_length=300)
    details: tuple[StatusDetail, ...] = ()


class SystemHealth(StrictContract):
    status: HealthStatus
    updated_at: datetime
    components: tuple[ComponentStatus, ...]


class WorkerStatus(StrictContract):
    worker_id: str = Field(min_length=1, max_length=128)
    queue: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    current_job_id: UUID | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    processed_jobs: int = Field(default=0, ge=0)
    failed_jobs: int = Field(default=0, ge=0)


class QueueStatus(StrictContract):
    name: str = Field(min_length=1, max_length=64)
    status: HealthStatus
    depth: int = Field(ge=0)
    active_workers: int = Field(ge=0)
    oldest_job_at: datetime | None = None
    updated_at: datetime


class SchedulerStatus(StrictContract):
    status: HealthStatus
    scan_interval_seconds: int = Field(ge=1)
    last_scan_at: datetime | None = None
    database_time: datetime | None = None
    automatic_scheduling_paused: bool
    pause_reason: str | None = Field(default=None, max_length=300)
    updated_at: datetime


class ScheduleOccurrence(StrictContract):
    occurrence_id: UUID
    occurrence_type: str = Field(min_length=1, max_length=64)
    definition_id: str = Field(min_length=1, max_length=128)
    scheduled_trade_date: date
    scheduled_at: datetime
    status: str = Field(min_length=1, max_length=32)
    job_id: UUID | None = None
    missed_reason: str | None = Field(default=None, max_length=300)
    created_at: datetime


class OccurrencePage(StrictContract):
    items: tuple[ScheduleOccurrence, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    total: int = Field(ge=0)


class ClockSourceStatus(StrictContract):
    source: str = Field(min_length=1, max_length=64)
    observed_at: datetime | None = None
    skew_seconds: float | None = None
    status: HealthStatus


class SystemClockStatus(StrictContract):
    status: HealthStatus
    application_time: datetime
    database_time: datetime | None
    max_skew_seconds: float | None
    automatic_scheduling_paused: bool
    sources: tuple[ClockSourceStatus, ...]
    updated_at: datetime


class ComponentStatusReader(Protocol):
    async def list_components(self) -> tuple[ComponentStatus, ...]: ...


class RuntimeStatusReader(Protocol):
    async def list_workers(self) -> tuple[WorkerStatus, ...]: ...

    async def list_queues(self) -> tuple[QueueStatus, ...]: ...


class SchedulerStatusReader(Protocol):
    async def get_status(self) -> SchedulerStatus: ...

    async def list_occurrences(
        self,
        *,
        page: int,
        page_size: int,
        occurrence_type: str | None,
        status: str | None,
        from_date: date | None,
        through_date: date | None,
    ) -> OccurrencePage: ...


class ClockStatusReader(Protocol):
    async def get_clock_status(self) -> SystemClockStatus: ...
