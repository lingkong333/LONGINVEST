from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class SectionStatus(StrEnum):
    OK = "OK"
    EMPTY = "EMPTY"
    WAITING = "WAITING"
    NON_TRADING_DAY = "NON_TRADING_DAY"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class DashboardStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


@dataclass(frozen=True, slots=True)
class SectionSnapshot:
    status: SectionStatus
    updated_at: datetime
    data: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    status: DashboardStatus
    generated_at: datetime
    sections: dict[str, SectionSnapshot]


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    id: str
    event_type: str
    object_type: str
    object_id: str
    title: str
    occurred_at: datetime
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DashboardTimeline:
    items: tuple[TimelineEntry, ...]
    generated_at: datetime


class DashboardSectionProvider(Protocol):
    @property
    def section_name(self) -> str: ...

    async def snapshot(self) -> SectionSnapshot: ...


class DashboardTimelineProvider(Protocol):
    async def timeline(
        self, *, limit: int, before: datetime | None
    ) -> tuple[TimelineEntry, ...]: ...


class DashboardTimelineUnavailable(Exception):
    """Raised when the composed timeline cannot be read within its deadline."""
