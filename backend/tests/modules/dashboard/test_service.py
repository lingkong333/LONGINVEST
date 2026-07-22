import asyncio
from datetime import UTC, datetime

import pytest

from long_invest.modules.dashboard.contracts import (
    DashboardStatus,
    DashboardTimelineUnavailable,
    SectionSnapshot,
    SectionStatus,
    TimelineEntry,
)
from long_invest.modules.dashboard.service import SECTION_NAMES, DashboardService

NOW = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)
pytestmark = pytest.mark.anyio


class FakeSectionProvider:
    def __init__(self, section_name, value) -> None:
        self.section_name = section_name
        self.value = value

    async def snapshot(self):
        if isinstance(self.value, Exception):
            raise self.value
        if self.value == "slow":
            await asyncio.sleep(1)
        return self.value


class FakeTimelineProvider:
    def __init__(self) -> None:
        self.items = (
            TimelineEntry(
                id="event-1",
                event_type="signal",
                object_type="subscription",
                object_id="sub-1",
                title="Signal zone changed",
                occurred_at=NOW,
                details={"after_zone": "LOW_WATCH"},
            ),
        )

    async def timeline(self, *, limit, before):
        return self.items[:limit]


def _service(
    overrides=None,
    *,
    section_timeout_seconds=1.0,
):
    values = {
        name: SectionSnapshot(SectionStatus.OK, NOW, {"count": 1})
        for name in SECTION_NAMES
    }
    values.update(overrides or {})
    timeline = FakeTimelineProvider()
    service = DashboardService(
        tuple(FakeSectionProvider(name, value) for name, value in values.items()),
        timeline,
        clock=lambda: NOW,
        section_timeout_seconds=section_timeout_seconds,
    )
    return service, timeline


async def test_summary_returns_all_sections_and_preserves_empty_states() -> None:
    service, _ = _service(
        {
            "quote_batches": SectionSnapshot(SectionStatus.NON_TRADING_DAY, NOW, {}),
            "signals": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        }
    )

    result = await service.summary()

    assert result.status is DashboardStatus.HEALTHY
    assert tuple(result.sections) == SECTION_NAMES
    assert result.sections["quote_batches"].status is SectionStatus.NON_TRADING_DAY
    assert result.sections["signals"].status is SectionStatus.EMPTY


async def test_summary_isolates_section_timeout_and_failure() -> None:
    service, _ = _service(
        {"providers": RuntimeError("database failed"), "jobs": "slow"},
        section_timeout_seconds=0.01,
    )

    result = await service.summary()

    assert result.status is DashboardStatus.DEGRADED
    assert result.sections["providers"].status is SectionStatus.ERROR
    assert result.sections["providers"].error == "SECTION_QUERY_FAILED"
    assert result.sections["jobs"].status is SectionStatus.TIMEOUT
    assert result.sections["monitoring"].status is SectionStatus.OK


async def test_summary_marks_critical_unresolved_alert_as_unhealthy() -> None:
    service, _ = _service(
        {
            "alerts": SectionSnapshot(
                SectionStatus.OK, NOW, {"critical": 1, "unresolved": 1}
            )
        }
    )

    result = await service.summary()

    assert result.status is DashboardStatus.UNHEALTHY


async def test_summary_does_not_show_problem_counts_as_healthy() -> None:
    service, _ = _service(
        {"jobs": SectionSnapshot(SectionStatus.OK, NOW, {"failed": 2, "timed_out": 0})}
    )

    result = await service.summary()

    assert result.status is DashboardStatus.DEGRADED
    assert result.sections["jobs"].status is SectionStatus.DEGRADED


async def test_summary_does_not_show_missing_calendar_as_healthy() -> None:
    service, _ = _service(
        {
            "infrastructure": SectionSnapshot(
                SectionStatus.EMPTY,
                NOW,
                {
                    "active_workers": 0,
                    "stale_workers": 0,
                    "calendar_covers_today": False,
                },
            )
        }
    )

    result = await service.summary()

    assert result.sections["infrastructure"].status is SectionStatus.DEGRADED


async def test_timeline_returns_bounded_repository_result() -> None:
    service, timeline = _service()

    result = await service.timeline(limit=1, before=NOW)

    assert result.generated_at == NOW
    assert result.items == timeline.items


async def test_summary_marks_missing_provider_without_hiding_other_sections() -> None:
    timeline = FakeTimelineProvider()
    service = DashboardService(
        (
            FakeSectionProvider(
                "signals", SectionSnapshot(SectionStatus.EMPTY, NOW, {})
            ),
        ),
        timeline,
        clock=lambda: NOW,
    )

    result = await service.summary()

    assert result.sections["signals"].status is SectionStatus.EMPTY
    assert result.sections["jobs"].error == "SECTION_PROVIDER_UNAVAILABLE"
    assert result.status is DashboardStatus.DEGRADED


async def test_timeline_converts_provider_failure_to_stable_boundary_error() -> None:
    class FailingTimelineProvider:
        async def timeline(self, *, limit, before):
            raise RuntimeError("internal provider failure")

    service = DashboardService((), FailingTimelineProvider(), clock=lambda: NOW)

    with pytest.raises(DashboardTimelineUnavailable):
        await service.timeline(limit=50, before=None)
