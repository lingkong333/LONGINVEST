from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from long_invest.modules.monitoring.scheduler import MonitorOccurrenceApplication
from long_invest.modules.system_status.contracts import (
    ClockSourceStatus,
    ComponentStatus,
    HealthStatus,
    OccurrencePage,
    QueueStatus,
    ScheduleOccurrence,
    SchedulerPlan,
    SchedulerStatus,
    StatusDetail,
    SystemClockStatus,
    WorkerStatus,
)
from long_invest.modules.system_status.runtime import SchedulerRuntimeApplication
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.admin import JobAdminService
from long_invest.platform.jobs.contracts import JobStatus


class ComponentStatusAdapter:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_components(self) -> tuple[ComponentStatus, ...]:
        now = datetime.now(UTC)
        database_ok, migration_ok = await asyncio.gather(
            _probe(self._database.ping),
            _probe(self._database.migration_is_current),
        )
        usage = shutil.disk_usage("/")
        free_ratio = usage.free / usage.total if usage.total else 0
        disk_status = (
            HealthStatus.HEALTHY if free_ratio >= 0.1 else HealthStatus.DEGRADED
        )
        return (
            ComponentStatus(
                name="postgresql",
                category="database",
                status=(
                    HealthStatus.HEALTHY
                    if database_ok and migration_ok
                    else HealthStatus.UNAVAILABLE
                ),
                critical=True,
                source="database-probe",
                updated_at=now,
                details=(StatusDetail(key="migration_current", value=migration_ok),),
            ),
            ComponentStatus(
                name="disk",
                category="storage",
                status=disk_status,
                source="local-filesystem",
                updated_at=now,
                details=(
                    StatusDetail(key="free_bytes", value=usage.free, unit="bytes"),
                    StatusDetail(
                        key="free_percent", value=round(free_ratio * 100, 2), unit="%"
                    ),
                ),
            ),
        )


class PostgresRuntimeStatusAdapter:
    def __init__(
        self,
        database: Database,
        runtime: SchedulerRuntimeApplication,
        *,
        stale_after_seconds: int = 30,
    ) -> None:
        self._database = database
        self._runtime = runtime
        self._stale_after_seconds = stale_after_seconds

    async def list_workers(self) -> tuple[WorkerStatus, ...]:
        now = datetime.now(UTC)
        runtime = await self._runtime.get()
        is_fresh = bool(
            runtime
            and now - runtime.heartbeat_at
            <= timedelta(seconds=self._stale_after_seconds)
        )
        async with self._database.session() as session:
            jobs = JobAdminService(session)
            running = await jobs.list_jobs(
                page=1,
                page_size=1,
                status=JobStatus.RUNNING,
                queue="postgres",
            )
            succeeded = await jobs.list_jobs(
                page=1,
                page_size=1,
                status=JobStatus.SUCCEEDED,
                queue="postgres",
            )
            failed = await jobs.list_jobs(
                page=1,
                page_size=1,
                status=JobStatus.FAILED,
                queue="postgres",
            )
        return (
            WorkerStatus(
                worker_id="ordinary-background",
                queue="postgres",
                status="RUNNING" if is_fresh else "UNAVAILABLE",
                current_job_id=(running.items[0].id if running.items else None),
                heartbeat_at=(runtime.heartbeat_at if runtime else None),
                processed_jobs=succeeded.total,
                failed_jobs=failed.total,
            ),
        )

    async def list_queues(self) -> tuple[QueueStatus, ...]:
        now = datetime.now(UTC)
        runtime = await self._runtime.get()
        is_fresh = bool(
            runtime
            and now - runtime.heartbeat_at
            <= timedelta(seconds=self._stale_after_seconds)
        )
        async with self._database.session() as session:
            jobs = JobAdminService(session)
            pending = await jobs.list_jobs(
                page=1,
                page_size=1,
                status=JobStatus.PENDING,
                queue="postgres",
            )
        return (
            QueueStatus(
                name="postgres",
                status=(
                    HealthStatus.HEALTHY if is_fresh else HealthStatus.UNAVAILABLE
                ),
                depth=pending.total,
                active_workers=1 if is_fresh else 0,
                oldest_job_at=None,
                updated_at=now,
            ),
        )


class SchedulerStatusAdapter:
    def __init__(
        self,
        database: Database,
        occurrences: MonitorOccurrenceApplication,
        runtime: SchedulerRuntimeApplication,
        *,
        scan_interval_seconds: int = 10,
        stale_after_seconds: int = 30,
    ) -> None:
        self._database = database
        self._occurrences = occurrences
        self._runtime = runtime
        self._scan_interval_seconds = scan_interval_seconds
        self._stale_after_seconds = stale_after_seconds

    async def get_status(self) -> SchedulerStatus:
        now = await self._database_time()
        runtime = await self._runtime.get()
        if runtime is None:
            return SchedulerStatus(
                status=HealthStatus.UNKNOWN,
                scan_interval_seconds=self._scan_interval_seconds,
                last_scan_at=None,
                database_time=now,
                automatic_scheduling_paused=True,
                pause_reason="scheduler heartbeat is not available",
                plans=(),
                next_run_at=None,
                updated_at=now,
            )
        stale = now - runtime.heartbeat_at > timedelta(
            seconds=self._stale_after_seconds
        )
        if stale:
            status = HealthStatus.UNAVAILABLE
            paused = True
            reason = "scheduler heartbeat is stale"
        elif runtime.automatic_scheduling_paused or runtime.consecutive_failures:
            status = HealthStatus.DEGRADED
            paused = runtime.automatic_scheduling_paused
            reason = runtime.pause_reason
        else:
            status = HealthStatus.HEALTHY
            paused = False
            reason = None
        plans = _scheduler_plans(runtime)
        return SchedulerStatus(
            status=status,
            scan_interval_seconds=self._scan_interval_seconds,
            last_scan_at=runtime.last_scan_at,
            database_time=now,
            automatic_scheduling_paused=paused,
            pause_reason=reason,
            plans=plans,
            next_run_at=min(
                (plan.next_run_at for plan in plans), default=None
            ),
            updated_at=now,
        )

    async def list_occurrences(self, **filters) -> OccurrencePage:
        page = await self._occurrences.list(**filters)
        return OccurrencePage(
            items=tuple(
                ScheduleOccurrence(
                    occurrence_id=item.id,
                    occurrence_type=item.occurrence_type,
                    definition_id=str(item.schedule_id or item.definition_key),
                    scheduled_trade_date=(
                        item.scheduled_trade_date
                        or (item.scheduled_at + timedelta(hours=8)).date()
                    ),
                    scheduled_at=item.scheduled_at,
                    status=item.status.value,
                    trigger_type=item.trigger_type,
                    expected_count=item.expected_count,
                    fetched_count=item.fetched_count,
                    failed_count=item.failed_count,
                    started_at=item.started_at,
                    completed_at=item.completed_at,
                    job_id=item.job_id,
                    missed_reason=item.error_code,
                    created_at=item.created_at or item.scheduled_at,
                )
                for item in page.items
            ),
            page=page.page,
            page_size=page.page_size,
            total=page.total,
        )

    async def _database_time(self) -> datetime:
        async with self._database.session() as session:
            value = await session.scalar(select(func.now()))
        if value is None:
            raise ConnectionError("database time is unavailable")
        return value


class ClockStatusAdapter:
    def __init__(
        self,
        database: Database,
        *,
        warning_skew_seconds: float = 5,
        pause_skew_seconds: float = 30,
    ) -> None:
        self._database = database
        self._warning_skew_seconds = warning_skew_seconds
        self._pause_skew_seconds = pause_skew_seconds

    async def get_clock_status(self) -> SystemClockStatus:
        application_time = datetime.now(UTC)
        async with self._database.session() as session:
            database_time = await session.scalar(select(func.now()))
        skew = (
            abs((application_time - database_time).total_seconds())
            if database_time is not None
            else None
        )
        status = (
            HealthStatus.HEALTHY
            if skew is not None and skew <= self._warning_skew_seconds
            else HealthStatus.DEGRADED
        )
        return SystemClockStatus(
            status=status,
            application_time=application_time,
            database_time=database_time,
            max_skew_seconds=skew,
            automatic_scheduling_paused=bool(
                skew is None or skew > self._pause_skew_seconds
            ),
            sources=(
                ClockSourceStatus(
                    source="database",
                    observed_at=database_time,
                    skew_seconds=skew,
                    status=status,
                ),
            ),
            updated_at=application_time,
        )


async def _probe(operation) -> bool:
    try:
        return bool(await operation())
    except Exception:
        return False


def _scheduler_plans(runtime) -> tuple[SchedulerPlan, ...]:
    values = []
    for kind, entries in (
        ("INTRADAY", getattr(runtime, "intraday_plan", ())),
        ("PERSISTENT", getattr(runtime, "persistent_plan", ())),
    ):
        for entry in entries:
            try:
                values.append(
                    SchedulerPlan(
                        key=entry["key"],
                        kind=kind,
                        next_run_at=datetime.fromisoformat(entry["next_run_at"]),
                        timezone=entry.get("timezone", "Asia/Shanghai"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return tuple(sorted(values, key=lambda item: item.next_run_at))
