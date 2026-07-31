from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.platform.jobs.contracts import SubmitPostgresJob
from long_invest.platform.jobs.postgres_service import PostgresJobService

SHANGHAI = ZoneInfo("Asia/Shanghai")
INTRADAY_JOB_PREFIX = "intraday:"
PERSISTENT_JOB_PREFIX = "persistent:"
logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PersistentSchedule:
    key: str
    job_type: str
    module_owner: str
    at: time
    priority: int
    trading_day_only: bool = True

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.job_type.strip():
            raise ValueError("persistent schedule identity is required")
        if self.at.tzinfo is not None or self.at.second or self.at.microsecond:
            raise ValueError("persistent schedule time must be an HH:mm local time")
        if not 0 <= self.priority <= 3:
            raise ValueError("persistent schedule priority is invalid")


DAILY_MARKET_DATA = PersistentSchedule(
    key="daily-market-data",
    job_type="DAILY_MARKET_DATA",
    module_owner="market_data",
    at=time(17),
    priority=1,
)


@dataclass(frozen=True, slots=True)
class DailyGapPlan:
    dates: tuple[date, ...]
    calendar_version_id: UUID


class DailyGapPlanner:
    def __init__(self, database, calendar) -> None:
        self._database = database
        self._calendar = calendar

    async def plan(self, *, before: date) -> DailyGapPlan | None:
        async with self._database.session() as session:
            repository = DailyDataRepository(session)
            bounds = await repository.batch_date_bounds()
        if bounds is None or bounds[0] >= before:
            return None
        window = await self._calendar.trading_dates(bounds[0], before)
        async with self._database.session() as session:
            existing = await DailyDataRepository(session).batch_dates(
                start=bounds[0], end=before
            )
        missing = tuple(item for item in window.dates if item not in existing)
        if not missing:
            return None
        return DailyGapPlan(missing, window.version_id)


class PostgresPersistentJobSubmitter:
    def __init__(self, database) -> None:
        self._database = database

    async def submit(
        self,
        plan: PersistentSchedule,
        *,
        trade_date: date,
        calendar_version_id,
        scheduled_at: datetime,
    ) -> None:
        day = trade_date.isoformat()
        command = SubmitPostgresJob(
            job_type=plan.job_type,
            module_owner=plan.module_owner,
            priority=plan.priority,
            idempotency_scope=f"scheduler:{plan.key}",
            idempotency_key=day,
            request_id=f"scheduler-{plan.key}-{day}",
            config_snapshot={
                "trade_date": day,
                "calendar_version_id": str(calendar_version_id),
                "scheduled_at": scheduled_at.isoformat(),
                "trigger": "AUTOMATIC",
            },
            business_object_type=plan.key,
            business_object_id=day,
            soft_timeout_seconds=3600,
            hard_timeout_seconds=7200,
            max_attempts=5,
            recoverable=True,
            max_recoveries=4,
        )
        async with self._database.transaction() as session:
            await PostgresJobService(session).submit(command, now=scheduled_at)

    async def submit_recovery(
        self,
        dates: tuple[date, ...],
        *,
        calendar_version_id: UUID,
        scheduled_at: datetime,
    ) -> None:
        encoded_dates = [item.isoformat() for item in dates]
        digest = sha256(",".join(encoded_dates).encode()).hexdigest()[:16]
        command = SubmitPostgresJob(
            job_type="DAILY_MARKET_RECOVERY",
            module_owner="market_data",
            priority=3,
            idempotency_scope="scheduler:daily-market-recovery",
            idempotency_key=f"{encoded_dates[0]}:{encoded_dates[-1]}:{digest}",
            request_id=f"scheduler-daily-recovery-{digest}",
            config_snapshot={
                "trade_dates": encoded_dates,
                "calendar_version_id": str(calendar_version_id),
                "scheduled_at": scheduled_at.isoformat(),
                "trigger": "STARTUP_RECOVERY",
                "concurrency": 4,
            },
            business_object_type="daily-market-recovery",
            business_object_id=digest,
            soft_timeout_seconds=82800,
            hard_timeout_seconds=86400,
            max_attempts=2,
            recoverable=True,
            max_recoveries=3,
        )
        async with self._database.transaction() as session:
            await PostgresJobService(session).submit(command, now=scheduled_at)


class DualPathScheduler:
    def __init__(
        self,
        *,
        calendar,
        schedules,
        runtime,
        persistent_submitter,
        intraday_handler: Callable[[datetime], Awaitable[None]],
        instance_id: str,
        daily_gap_planner=None,
        persistent_plans: Sequence[PersistentSchedule] = (DAILY_MARKET_DATA,),
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self._calendar = calendar
        self._schedules = schedules
        self._runtime = runtime
        self._persistent_submitter = persistent_submitter
        self._intraday_handler = intraday_handler
        self._instance_id = instance_id
        self._daily_gap_planner = daily_gap_planner
        self._persistent_plans = tuple(persistent_plans)
        self._scheduler = scheduler or AsyncIOScheduler(timezone=SHANGHAI)
        self._refresh_lock = asyncio.Lock()
        self._started = False

    async def start(self, *, now: datetime | None = None) -> None:
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        self._install_fixed_triggers()
        await self.refresh(now=now)
        await self.recover_persistent(now=now)

    async def stop(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False

    async def refresh(self, *, now: datetime | None = None) -> None:
        async with self._refresh_lock:
            decision = await self._runtime.begin_scan(
                instance_id=self._instance_id,
                application_time=now or datetime.now(UTC),
            )
            self._remove_intraday_triggers()
            failures = 0
            if not decision.automatic_scheduling_paused:
                failures = await self._register_today_intraday(decision.database_time)
            await self._record_runtime(
                success=failures == 0,
                error_code=("INTRADAY_PLAN_REFRESH_PARTIAL" if failures else None),
            )

    async def recover_persistent(self, *, now: datetime | None = None) -> None:
        decision = await self._runtime.begin_scan(
            instance_id=self._instance_id,
            application_time=now or datetime.now(UTC),
        )
        if decision.automatic_scheduling_paused:
            await self._record_runtime(success=True)
            return
        local_now = decision.database_time.astimezone(SHANGHAI)
        failures = 0
        if self._daily_gap_planner is not None:
            try:
                gap = await self._daily_gap_planner.plan(
                    before=local_now.date() - timedelta(days=1)
                )
                if gap is not None:
                    await self._persistent_submitter.submit_recovery(
                        gap.dates,
                        calendar_version_id=gap.calendar_version_id,
                        scheduled_at=decision.database_time,
                    )
            except Exception:
                failures += 1
                logger.exception("persistent_recovery_gap_failed", category="scheduler")
        for plan in self._persistent_plans:
            scheduled_at = datetime.combine(
                local_now.date(), plan.at, tzinfo=SHANGHAI
            ).astimezone(UTC)
            if scheduled_at <= decision.database_time:
                try:
                    await self._submit_persistent(plan, scheduled_at)
                except Exception:
                    failures += 1
                    logger.exception(
                        "persistent_recovery_plan_failed",
                        category="scheduler",
                        plan=plan.key,
                    )
        await self._record_runtime(
            success=failures == 0,
            error_code="PERSISTENT_RECOVERY_PARTIAL" if failures else None,
        )

    async def heartbeat(self) -> None:
        await self._runtime.begin_scan(
            instance_id=self._instance_id,
            application_time=datetime.now(UTC),
        )
        await self._record_runtime(success=True)

    def plan_snapshot(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        intraday = []
        persistent = []
        for job in self._scheduler.get_jobs():
            if job.next_run_time is None:
                continue
            item = {
                "key": job.id.split(":", 1)[1],
                "next_run_at": job.next_run_time.astimezone(UTC).isoformat(),
                "timezone": "Asia/Shanghai",
            }
            if job.id.startswith(INTRADAY_JOB_PREFIX):
                intraday.append(item)
            elif job.id.startswith(PERSISTENT_JOB_PREFIX):
                persistent.append(item)
        return (
            sorted(intraday, key=lambda item: item["next_run_at"]),
            sorted(persistent, key=lambda item: item["next_run_at"]),
        )

    def _install_fixed_triggers(self) -> None:
        self._scheduler.add_job(
            self.refresh,
            CronTrigger(hour=0, minute=0, timezone=SHANGHAI),
            id="internal:midnight-refresh",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        for plan in self._persistent_plans:
            self._scheduler.add_job(
                self._fire_persistent,
                CronTrigger(
                    hour=plan.at.hour,
                    minute=plan.at.minute,
                    timezone=SHANGHAI,
                ),
                kwargs={"plan": plan},
                id=f"{PERSISTENT_JOB_PREFIX}{plan.key}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=60,
            )

    async def _register_today_intraday(self, now: datetime) -> int:
        local_date = now.astimezone(SHANGHAI).date()
        window = await self._calendar.trading_dates(local_date, local_date)
        if local_date not in window.dates:
            return 0
        times = set()
        failures = 0
        for schedule in await self._schedules.list():
            try:
                revision = await self._schedules.current_revision(schedule.id)
                times.update(revision.times)
            except Exception:
                failures += 1
                logger.exception(
                    "intraday_schedule_config_failed",
                    category="scheduler",
                    schedule_id=str(schedule.id),
                )
        for local_time in sorted(times):
            run_at = datetime.combine(
                local_date, local_time, tzinfo=SHANGHAI
            ).astimezone(UTC)
            if run_at <= now:
                continue
            key = run_at.astimezone(SHANGHAI).strftime("%Y-%m-%dT%H:%M")
            self._scheduler.add_job(
                self._fire_intraday,
                DateTrigger(run_date=run_at),
                kwargs={"scheduled_at": run_at},
                id=f"{INTRADAY_JOB_PREFIX}{key}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=1,
            )
        return failures

    async def _fire_intraday(self, *, scheduled_at: datetime) -> None:
        try:
            decision = await self._runtime.begin_scan(
                instance_id=self._instance_id,
                application_time=datetime.now(UTC),
            )
            if decision.automatic_scheduling_paused:
                return
            if decision.database_time > scheduled_at + timedelta(seconds=60):
                return
            local_date = scheduled_at.astimezone(SHANGHAI).date()
            window = await self._calendar.trading_dates(local_date, local_date)
            if local_date not in window.dates:
                return
            await self._intraday_handler(scheduled_at)
        except Exception:
            logger.exception("intraday_schedule_failed", category="scheduler")
            await self._record_runtime(
                success=False, error_code="INTRADAY_SCHEDULE_FAILED"
            )
        else:
            await self._record_runtime(success=True)

    async def _fire_persistent(self, *, plan: PersistentSchedule) -> None:
        try:
            decision = await self._runtime.begin_scan(
                instance_id=self._instance_id,
                application_time=datetime.now(UTC),
            )
            if decision.automatic_scheduling_paused:
                return
            local_date = decision.database_time.astimezone(SHANGHAI).date()
            scheduled_at = datetime.combine(
                local_date, plan.at, tzinfo=SHANGHAI
            ).astimezone(UTC)
            if decision.database_time < scheduled_at:
                return
            await self._submit_persistent(plan, scheduled_at)
        except Exception:
            logger.exception("persistent_schedule_failed", category="scheduler")
            await self._record_runtime(
                success=False, error_code="PERSISTENT_SCHEDULE_FAILED"
            )
        else:
            await self._record_runtime(success=True)

    async def _submit_persistent(
        self, plan: PersistentSchedule, scheduled_at: datetime
    ) -> None:
        trade_date = scheduled_at.astimezone(SHANGHAI).date()
        window = await self._calendar.trading_dates(trade_date, trade_date)
        if plan.trading_day_only and trade_date not in window.dates:
            return
        await self._persistent_submitter.submit(
            plan,
            trade_date=trade_date,
            calendar_version_id=window.version_id,
            scheduled_at=scheduled_at,
        )

    def _remove_intraday_triggers(self) -> None:
        for job in self._scheduler.get_jobs():
            if job.id.startswith(INTRADAY_JOB_PREFIX):
                self._scheduler.remove_job(job.id)

    async def _record_runtime(
        self,
        *,
        success: bool,
        error_code: str | None = None,
    ) -> None:
        intraday, persistent = self.plan_snapshot()
        await self._runtime.finish_scan(
            instance_id=self._instance_id,
            success=success,
            error_code=error_code,
            intraday_plan=intraday,
            persistent_plan=persistent,
        )
