from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert

from long_invest.modules.system_status.models import SchedulerRuntimeState


@dataclass(frozen=True, slots=True)
class SchedulerScanDecision:
    database_time: datetime
    clock_skew_seconds: float
    automatic_scheduling_paused: bool
    pause_reason: str | None


@dataclass(frozen=True, slots=True)
class SchedulerRuntimeSnapshot:
    heartbeat_at: datetime
    last_scan_at: datetime | None
    consecutive_failures: int
    clock_skew_seconds: float
    automatic_scheduling_paused: bool
    pause_reason: str | None


class SchedulerRuntimeRepository:
    def __init__(self, session) -> None:
        self._session = session

    async def database_time(self) -> datetime:
        value = await self._session.scalar(select(func.now()))
        if value is None:
            raise ConnectionError("database time is unavailable")
        return value

    async def begin_scan(
        self,
        *,
        role: str,
        instance_id: str,
        database_time: datetime,
        clock_skew_seconds: float,
        paused: bool,
        pause_reason: str | None,
    ) -> None:
        statement = insert(SchedulerRuntimeState).values(
            role=role,
            instance_id=instance_id,
            started_at=database_time,
            heartbeat_at=database_time,
            clock_skew_seconds=clock_skew_seconds,
            automatic_scheduling_paused=paused,
            pause_reason=pause_reason,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[SchedulerRuntimeState.role],
                set_={
                    "instance_id": instance_id,
                    "started_at": case(
                        (
                            SchedulerRuntimeState.instance_id != instance_id,
                            database_time,
                        ),
                        else_=SchedulerRuntimeState.started_at,
                    ),
                    "heartbeat_at": database_time,
                    "clock_skew_seconds": clock_skew_seconds,
                    "automatic_scheduling_paused": paused,
                    "pause_reason": pause_reason,
                    "updated_at": database_time,
                },
            )
        )

    async def finish_scan(
        self,
        *,
        role: str,
        instance_id: str,
        database_time: datetime,
        success: bool,
        error_code: str | None,
    ) -> None:
        values = {
            "heartbeat_at": database_time,
            "last_scan_at": database_time,
            "updated_at": database_time,
            "last_error_code": None if success else error_code,
            "consecutive_failures": (
                0
                if success
                else SchedulerRuntimeState.consecutive_failures + 1
            ),
        }
        if success:
            values["last_success_at"] = database_time
        await self._session.execute(
            update(SchedulerRuntimeState)
            .where(
                SchedulerRuntimeState.role == role,
                SchedulerRuntimeState.instance_id == instance_id,
            )
            .values(**values)
        )

    async def get(self, role: str) -> SchedulerRuntimeSnapshot | None:
        row = await self._session.scalar(
            select(SchedulerRuntimeState).where(SchedulerRuntimeState.role == role)
        )
        if row is None:
            return None
        return SchedulerRuntimeSnapshot(
            heartbeat_at=row.heartbeat_at,
            last_scan_at=row.last_scan_at,
            consecutive_failures=row.consecutive_failures,
            clock_skew_seconds=row.clock_skew_seconds,
            automatic_scheduling_paused=row.automatic_scheduling_paused,
            pause_reason=row.pause_reason,
        )


class SchedulerRuntimeApplication:
    def __init__(
        self,
        database,
        *,
        role: str = "monitor-scheduler",
        pause_skew_seconds: float = 30,
        repository_factory: Callable[
            ..., SchedulerRuntimeRepository
        ] = SchedulerRuntimeRepository,
    ) -> None:
        self._database = database
        self._role = role
        self._pause_skew_seconds = pause_skew_seconds
        self._repository_factory = repository_factory

    async def begin_scan(
        self, *, instance_id: str, application_time: datetime
    ) -> SchedulerScanDecision:
        async with self._database.transaction() as session:
            repository = self._repository_factory(session)
            database_time = await repository.database_time()
            skew = abs((application_time - database_time).total_seconds())
            paused = skew > self._pause_skew_seconds
            reason = (
                "application and database clocks differ by more than 30 seconds"
                if paused
                else None
            )
            await repository.begin_scan(
                role=self._role,
                instance_id=instance_id,
                database_time=database_time,
                clock_skew_seconds=skew,
                paused=paused,
                pause_reason=reason,
            )
        return SchedulerScanDecision(database_time, skew, paused, reason)

    async def finish_scan(
        self,
        *,
        instance_id: str,
        success: bool,
        error_code: str | None = None,
    ) -> None:
        async with self._database.transaction() as session:
            repository = self._repository_factory(session)
            database_time = await repository.database_time()
            await repository.finish_scan(
                role=self._role,
                instance_id=instance_id,
                database_time=database_time,
                success=success,
                error_code=error_code,
            )

    async def get(self) -> SchedulerRuntimeSnapshot | None:
        async with self._database.session() as session:
            return await self._repository_factory(session).get(self._role)
