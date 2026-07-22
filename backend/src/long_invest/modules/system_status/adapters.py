from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis import Redis
from rq import Queue, Worker
from sqlalchemy import func, select

from long_invest.modules.monitoring.scheduler import MonitorOccurrenceApplication
from long_invest.modules.system_status.contracts import (
    ClockSourceStatus,
    ComponentStatus,
    HealthStatus,
    OccurrencePage,
    QueueStatus,
    ScheduleOccurrence,
    SchedulerStatus,
    StatusDetail,
    SystemClockStatus,
    WorkerStatus,
)
from long_invest.platform.cache.redis import RedisProbe
from long_invest.platform.database.engine import Database


class ComponentStatusAdapter:
    def __init__(self, database: Database, redis: RedisProbe) -> None:
        self._database = database
        self._redis = redis

    async def list_components(self) -> tuple[ComponentStatus, ...]:
        now = datetime.now(UTC)
        database_ok, migration_ok, redis_ok = await asyncio.gather(
            _probe(self._database.ping),
            _probe(self._database.migration_is_current),
            _probe(self._redis.ping),
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
                name="redis",
                category="queue",
                status=(HealthStatus.HEALTHY if redis_ok else HealthStatus.UNAVAILABLE),
                source="redis-probe",
                updated_at=now,
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


class RqRuntimeStatusAdapter:
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url

    async def list_workers(self) -> tuple[WorkerStatus, ...]:
        return await asyncio.to_thread(self._workers)

    async def list_queues(self) -> tuple[QueueStatus, ...]:
        return await asyncio.to_thread(self._queues)

    def _workers(self) -> tuple[WorkerStatus, ...]:
        connection = Redis.from_url(self._redis_url)
        try:
            result = []
            for worker in Worker.all(connection=connection):
                current_job_id = _uuid_or_none(getattr(worker, "_job_id", None))
                queues = tuple(queue.name for queue in worker.queues) or ("unknown",)
                for queue_name in queues:
                    result.append(
                        WorkerStatus(
                            worker_id=worker.name,
                            queue=queue_name,
                            status=str(
                                getattr(worker.state, "value", worker.state)
                            ).upper(),
                            current_job_id=current_job_id,
                            started_at=getattr(worker, "birth_date", None),
                            heartbeat_at=getattr(worker, "last_heartbeat", None),
                            processed_jobs=int(
                                getattr(worker, "successful_job_count", 0)
                            ),
                            failed_jobs=int(getattr(worker, "failed_job_count", 0)),
                        )
                    )
            return tuple(result)
        finally:
            connection.close()

    def _queues(self) -> tuple[QueueStatus, ...]:
        connection = Redis.from_url(self._redis_url)
        try:
            now = datetime.now(UTC)
            workers = Worker.all(connection=connection)
            active_by_queue: dict[str, int] = {}
            for worker in workers:
                for queue in worker.queues:
                    active_by_queue[queue.name] = active_by_queue.get(queue.name, 0) + 1
            result = []
            for queue in Queue.all(connection=connection):
                active = active_by_queue.get(queue.name, 0)
                status = (
                    HealthStatus.HEALTHY
                    if active > 0 or queue.count == 0
                    else HealthStatus.DEGRADED
                )
                jobs = queue.get_jobs(offset=0, length=1)
                result.append(
                    QueueStatus(
                        name=queue.name,
                        status=status,
                        depth=queue.count,
                        active_workers=active,
                        oldest_job_at=jobs[0].enqueued_at if jobs else None,
                        updated_at=now,
                    )
                )
            return tuple(sorted(result, key=lambda item: item.name))
        finally:
            connection.close()


class SchedulerStatusAdapter:
    def __init__(
        self,
        database: Database,
        occurrences: MonitorOccurrenceApplication,
        *,
        scan_interval_seconds: int = 10,
    ) -> None:
        self._database = database
        self._occurrences = occurrences
        self._scan_interval_seconds = scan_interval_seconds

    async def get_status(self) -> SchedulerStatus:
        now = await self._database_time()
        return SchedulerStatus(
            status=HealthStatus.UNKNOWN,
            scan_interval_seconds=self._scan_interval_seconds,
            last_scan_at=None,
            database_time=now,
            automatic_scheduling_paused=False,
            pause_reason="scheduler heartbeat is not available",
            updated_at=now,
        )

    async def list_occurrences(self, **filters) -> OccurrencePage:
        page = await self._occurrences.list(**filters)
        return OccurrencePage(
            items=tuple(
                ScheduleOccurrence(
                    occurrence_id=item.id,
                    occurrence_type=item.occurrence_type,
                    definition_id=str(item.schedule_id),
                    scheduled_trade_date=(
                        item.scheduled_at + timedelta(hours=8)
                    ).date(),
                    scheduled_at=item.scheduled_at,
                    status=item.status.value,
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
    def __init__(self, database: Database, *, max_skew_seconds: float = 5) -> None:
        self._database = database
        self._max_skew_seconds = max_skew_seconds

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
            if skew is not None and skew <= self._max_skew_seconds
            else HealthStatus.DEGRADED
        )
        return SystemClockStatus(
            status=status,
            application_time=application_time,
            database_time=database_time,
            max_skew_seconds=skew,
            automatic_scheduling_paused=bool(
                skew is None or skew > self._max_skew_seconds
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


def _uuid_or_none(value) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None
