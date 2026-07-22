from datetime import UTC, date, datetime

import pytest

from long_invest.modules.system_status.application import SystemStatusApplication
from long_invest.modules.system_status.contracts import (
    ClockSourceStatus,
    ComponentStatus,
    HealthStatus,
    OccurrencePage,
    QueueStatus,
    SchedulerStatus,
    SystemClockStatus,
    WorkerStatus,
)
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 22, 8, tzinfo=UTC)


class Components:
    def __init__(self, items=(), error: Exception | None = None) -> None:
        self.items = items
        self.error = error

    async def list_components(self):
        if self.error:
            raise self.error
        return self.items


class Runtime:
    async def list_workers(self):
        return (
            WorkerStatus(
                worker_id="worker-1",
                queue="realtime",
                status="IDLE",
                heartbeat_at=NOW,
            ),
        )

    async def list_queues(self):
        return (
            QueueStatus(
                name="realtime",
                status=HealthStatus.HEALTHY,
                depth=0,
                active_workers=1,
                updated_at=NOW,
            ),
        )


class Scheduler:
    async def get_status(self):
        return SchedulerStatus(
            status=HealthStatus.HEALTHY,
            scan_interval_seconds=10,
            automatic_scheduling_paused=False,
            updated_at=NOW,
        )

    async def list_occurrences(self, **filters):
        self.filters = filters
        return OccurrencePage(items=(), page=filters["page"], page_size=50, total=0)


class Clock:
    async def get_clock_status(self):
        return SystemClockStatus(
            status=HealthStatus.HEALTHY,
            application_time=NOW,
            database_time=NOW,
            max_skew_seconds=0,
            automatic_scheduling_paused=False,
            sources=(
                ClockSourceStatus(
                    source="database",
                    observed_at=NOW,
                    skew_seconds=0,
                    status=HealthStatus.HEALTHY,
                ),
            ),
            updated_at=NOW,
        )


def application(components: Components) -> SystemStatusApplication:
    return SystemStatusApplication(
        components=components,
        runtime=Runtime(),
        scheduler=Scheduler(),
        clock=Clock(),
    )


@pytest.mark.anyio
async def test_health_is_unavailable_when_a_critical_component_is_down() -> None:
    report = await application(
        Components(
            (
                ComponentStatus(
                    name="postgresql",
                    category="database",
                    status=HealthStatus.UNAVAILABLE,
                    critical=True,
                    source="database-probe",
                    updated_at=NOW,
                ),
                ComponentStatus(
                    name="redis",
                    category="cache",
                    status=HealthStatus.HEALTHY,
                    source="redis-probe",
                    updated_at=NOW,
                ),
            )
        )
    ).get_health()

    assert report.status is HealthStatus.UNAVAILABLE
    assert report.updated_at == NOW


@pytest.mark.anyio
async def test_health_is_degraded_for_a_noncritical_failure() -> None:
    report = await application(
        Components(
            (
                ComponentStatus(
                    name="redis",
                    category="cache",
                    status=HealthStatus.UNAVAILABLE,
                    source="redis-probe",
                    updated_at=NOW,
                ),
            )
        )
    ).get_health()

    assert report.status is HealthStatus.DEGRADED


@pytest.mark.anyio
async def test_health_rejects_empty_status_instead_of_showing_green() -> None:
    with pytest.raises(AppError) as captured:
        await application(Components()).get_health()

    assert captured.value.code == "SYSTEM_STATUS_EMPTY"
    assert captured.value.status_code == 503


@pytest.mark.anyio
async def test_dependency_failure_becomes_stable_service_error() -> None:
    with pytest.raises(AppError) as captured:
        await application(Components(error=ConnectionError("down"))).list_components()

    assert captured.value.code == "SYSTEM_STATUS_BACKEND_UNAVAILABLE"
    assert captured.value.status_code == 503


@pytest.mark.anyio
async def test_occurrence_date_range_is_validated_before_reader_call() -> None:
    with pytest.raises(AppError) as captured:
        await application(Components()).list_occurrences(
            page=1,
            page_size=50,
            occurrence_type=None,
            status=None,
            from_date=date(2026, 7, 23),
            through_date=date(2026, 7, 22),
        )

    assert captured.value.code == "SCHEDULE_FILTER_INVALID"


@pytest.mark.anyio
async def test_runtime_scheduler_and_clock_are_exposed_through_public_ports() -> None:
    app = application(Components())

    assert (await app.list_workers())[0].worker_id == "worker-1"
    assert (await app.list_queues())[0].name == "realtime"
    assert (await app.get_scheduler_status()).scan_interval_seconds == 10
    assert (await app.get_clock_status()).database_time == NOW
