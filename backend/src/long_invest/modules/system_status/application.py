from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date

from long_invest.modules.system_status.contracts import (
    ClockStatusReader,
    ComponentStatus,
    ComponentStatusReader,
    HealthStatus,
    OccurrencePage,
    QueueStatus,
    RuntimeStatusReader,
    SchedulerStatus,
    SchedulerStatusReader,
    SystemClockStatus,
    SystemHealth,
    WorkerStatus,
)
from long_invest.platform.errors import AppError


class SystemStatusApplication:
    def __init__(
        self,
        *,
        components: ComponentStatusReader,
        runtime: RuntimeStatusReader,
        scheduler: SchedulerStatusReader,
        clock: ClockStatusReader,
    ) -> None:
        self._components = components
        self._runtime = runtime
        self._scheduler = scheduler
        self._clock = clock

    async def get_health(self) -> SystemHealth:
        components = await self.list_components()
        if not components:
            raise AppError(
                code="SYSTEM_STATUS_EMPTY",
                message="系统状态数据暂不可用",
                status_code=503,
            )
        status = _overall_status(components)
        return SystemHealth(
            status=status,
            updated_at=max(item.updated_at for item in components),
            components=components,
        )

    async def list_components(self) -> tuple[ComponentStatus, ...]:
        return await _read(self._components.list_components)

    async def list_workers(self) -> tuple[WorkerStatus, ...]:
        return await _read(self._runtime.list_workers)

    async def list_queues(self) -> tuple[QueueStatus, ...]:
        return await _read(self._runtime.list_queues)

    async def get_scheduler_status(self) -> SchedulerStatus:
        return await _read(self._scheduler.get_status)

    async def list_occurrences(
        self,
        *,
        page: int,
        page_size: int,
        occurrence_type: str | None,
        status: str | None,
        from_date: date | None,
        through_date: date | None,
    ) -> OccurrencePage:
        if (
            from_date is not None
            and through_date is not None
            and from_date > through_date
        ):
            raise AppError(
                code="SCHEDULE_FILTER_INVALID",
                message="计划发生记录的日期范围无效",
                status_code=422,
            )
        return await _read(
            lambda: self._scheduler.list_occurrences(
                page=page,
                page_size=page_size,
                occurrence_type=occurrence_type,
                status=status,
                from_date=from_date,
                through_date=through_date,
            )
        )

    async def get_clock_status(self) -> SystemClockStatus:
        return await _read(self._clock.get_clock_status)


async def _read[T](operation: Callable[[], Awaitable[T]]) -> T:
    try:
        return await operation()
    except AppError:
        raise
    except (ConnectionError, TimeoutError, OSError) as exc:
        raise AppError(
            code="SYSTEM_STATUS_BACKEND_UNAVAILABLE",
            message="系统运行状态暂时无法读取",
            status_code=503,
        ) from exc


def _overall_status(components: tuple[ComponentStatus, ...]) -> HealthStatus:
    if any(
        item.critical and item.status is HealthStatus.UNAVAILABLE for item in components
    ):
        return HealthStatus.UNAVAILABLE
    if any(item.status is not HealthStatus.HEALTHY for item in components):
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY
