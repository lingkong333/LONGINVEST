from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog

from long_invest.modules.dashboard.contracts import (
    DashboardSectionProvider,
    DashboardStatus,
    DashboardSummary,
    DashboardTimeline,
    DashboardTimelineProvider,
    DashboardTimelineUnavailable,
    SectionSnapshot,
    SectionStatus,
)

SECTION_TIMEOUT_SECONDS = 1.0
SECTION_NAMES = (
    "system",
    "quote_batches",
    "monitoring",
    "positions",
    "signals",
    "daily_data",
    "targets",
    "jobs",
    "notifications",
    "providers",
    "infrastructure",
    "alerts",
)
logger = structlog.get_logger(__name__)


class DashboardService:
    def __init__(
        self,
        section_providers: tuple[DashboardSectionProvider, ...],
        timeline_provider: DashboardTimelineProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        section_timeout_seconds: float = SECTION_TIMEOUT_SECONDS,
    ) -> None:
        self._section_providers = {
            provider.section_name: provider for provider in section_providers
        }
        unknown = set(self._section_providers).difference(SECTION_NAMES)
        if unknown:
            raise ValueError(f"unsupported dashboard sections: {sorted(unknown)}")
        if len(self._section_providers) != len(section_providers):
            raise ValueError("dashboard section providers must have unique names")
        self._timeline_provider = timeline_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._section_timeout_seconds = section_timeout_seconds

    async def summary(self) -> DashboardSummary:
        results = await asyncio.gather(
            *(self._read_section(name) for name in SECTION_NAMES)
        )
        sections = dict(zip(SECTION_NAMES, results, strict=True))
        return DashboardSummary(
            status=_overall_status(sections),
            generated_at=self._clock(),
            sections=sections,
        )

    async def timeline(
        self,
        *,
        limit: int,
        before: datetime | None,
    ) -> DashboardTimeline:
        try:
            items = await asyncio.wait_for(
                self._timeline_provider.timeline(limit=limit, before=before),
                timeout=self._section_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "dashboard_timeline_query_failed", error_type=type(exc).__name__
            )
            raise DashboardTimelineUnavailable from exc
        return DashboardTimeline(items=items, generated_at=self._clock())

    async def _read_section(self, name: str) -> SectionSnapshot:
        provider = self._section_providers.get(name)
        if provider is None:
            return SectionSnapshot(
                status=SectionStatus.ERROR,
                updated_at=self._clock(),
                data={},
                error="SECTION_PROVIDER_UNAVAILABLE",
            )
        try:
            section = await asyncio.wait_for(
                provider.snapshot(), timeout=self._section_timeout_seconds
            )
            return _business_status(name, section, self._clock())
        except TimeoutError:
            return SectionSnapshot(
                status=SectionStatus.TIMEOUT,
                updated_at=self._clock(),
                data={},
                error="SECTION_TIMEOUT",
            )
        except Exception as exc:
            logger.warning(
                "dashboard_section_query_failed",
                section=name,
                error_type=type(exc).__name__,
            )
            return SectionSnapshot(
                status=SectionStatus.ERROR,
                updated_at=self._clock(),
                data={},
                error="SECTION_QUERY_FAILED",
            )


def _business_status(
    name: str, section: SectionSnapshot, observed_at: datetime
) -> SectionSnapshot:
    infrastructure_missing = (
        name == "infrastructure" and section.data.get("calendar_covers_today") is False
    )
    if section.status is not SectionStatus.OK and not infrastructure_missing:
        return section
    degraded_fields = {
        "quote_batches": ("missing_count", "conflict_count", "failed_count"),
        "monitoring": ("missing_state",),
        "daily_data": ("missing_count", "failed_count"),
        "targets": ("attention",),
        "jobs": ("failed", "timed_out"),
        "notifications": ("failed",),
        "providers": ("open_circuits",),
        "infrastructure": ("stale_workers",),
        "alerts": ("critical", "errors"),
    }
    has_problem = any(
        isinstance(section.data.get(field), int) and section.data[field] > 0
        for field in degraded_fields.get(name, ())
    )
    if name == "providers":
        total = section.data.get("total")
        healthy = section.data.get("healthy")
        has_problem = has_problem or (
            isinstance(total, int) and isinstance(healthy, int) and healthy < total
        )
    if name == "infrastructure":
        active_workers = section.data.get("active_workers")
        has_problem = (
            has_problem
            or infrastructure_missing
            or active_workers == 0
            or (
                isinstance(active_workers, int)
                and active_workers > 0
                and observed_at - section.updated_at > timedelta(minutes=2)
            )
        )
    if name == "providers" and section.data.get("total"):
        has_problem = has_problem or observed_at - section.updated_at > timedelta(
            minutes=10
        )
    if not has_problem:
        return section
    return SectionSnapshot(
        status=SectionStatus.DEGRADED,
        updated_at=section.updated_at,
        data=section.data,
        error=None,
    )


def _overall_status(sections: dict[str, SectionSnapshot]) -> DashboardStatus:
    if any(
        section.status in {SectionStatus.ERROR, SectionStatus.TIMEOUT}
        for section in sections.values()
    ):
        return DashboardStatus.DEGRADED
    critical_alerts = sections["alerts"].data.get("critical", 0)
    if isinstance(critical_alerts, int) and critical_alerts > 0:
        return DashboardStatus.UNHEALTHY
    if any(section.status is SectionStatus.DEGRADED for section in sections.values()):
        return DashboardStatus.DEGRADED
    return DashboardStatus.HEALTHY
