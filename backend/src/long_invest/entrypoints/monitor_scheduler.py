import asyncio
import os
import signal
import socket
from contextlib import suppress
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

import structlog

from long_invest.bootstrap.backtest_postgres_jobs import (
    backtest_batch,
    backtest_single,
)
from long_invest.bootstrap.history_backfills import (
    build_history_backfill_job_handler,
)
from long_invest.bootstrap.jobs import (
    qfq_refresh,
    security_master_refresh,
    signal_evaluate_batch,
    signal_reevaluate,
)
from long_invest.bootstrap.providers import (
    build_provider_service,
    close_provider_resources,
)
from long_invest.bootstrap.realtime_quotes import get_realtime_quote_runtime
from long_invest.bootstrap.stage4_runtime import (
    build_strategy_validation_executor,
)
from long_invest.modules.calendar.application import CalendarApplication
from long_invest.modules.daily_data.jobs import (
    DailyMarketRecoveryJob,
    FullMarketDailyJob,
)
from long_invest.modules.monitor_schedules.application import MonitorScheduleApplication
from long_invest.modules.monitoring.application import MonitorSubscriptionApplication
from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.runtime import NotificationDeliveryRuntime
from long_invest.modules.quotes.contracts import RealtimeCheckMode
from long_invest.modules.scheduling.runtime import (
    DailyGapPlanner,
    DualPathScheduler,
    PostgresPersistentJobSubmitter,
)
from long_invest.modules.securities.application import SecurityApplication
from long_invest.modules.strategies.jobs import (
    configure_strategy_validation_executor,
    strategy_publish,
    strategy_validate,
)
from long_invest.modules.system_status.runtime import SchedulerRuntimeApplication
from long_invest.modules.targets.jobs import target_calculate
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database
from long_invest.platform.database.notifications import PostgresNotificationListener
from long_invest.platform.jobs.postgres_runner import PostgresJobRunner
from long_invest.platform.logging.configure import configure_logging

HEARTBEAT_SECONDS = 5
logger = structlog.get_logger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def build_market_data_handlers(database: Database):
    return {
        "DAILY_MARKET_DATA": FullMarketDailyJob(
            database,
            provider_service_factory=build_provider_service,
        ),
        "DAILY_MARKET_RECOVERY": DailyMarketRecoveryJob(
            database,
            provider_service_factory=build_provider_service,
        ),
        "QFQ_REFRESH": qfq_refresh,
        "SECURITY_MASTER_REFRESH": security_master_refresh,
        "SIGNAL_EVALUATE_BATCH": signal_evaluate_batch,
        "SIGNAL_REEVALUATE": signal_reevaluate,
        "TARGET_CALCULATE": target_calculate,
        "STRATEGY_VALIDATE": strategy_validate,
        "STRATEGY_PUBLISH": strategy_publish,
        "BACKTEST_SINGLE": backtest_single,
        "BACKTEST_BATCH": backtest_batch,
    }


async def _run_intraday(
    scheduled_at: datetime,
    *,
    schedules: MonitorScheduleApplication,
    subscriptions: MonitorSubscriptionApplication,
) -> None:
    local_time = scheduled_at.astimezone(SHANGHAI).time().replace(tzinfo=None)
    grouped = {
        item.schedule_id: item.subscriptions
        for item in await subscriptions.enabled_schedule_snapshots()
    }
    selected = {}
    for schedule in await schedules.list():
        current = grouped.get(schedule.id, ())
        if not current:
            continue
        try:
            revision = await schedules.current_revision(schedule.id)
        except Exception:
            logger.exception(
                "intraday_schedule_scope_failed",
                category="scheduler",
                schedule_id=str(schedule.id),
            )
            continue
        if local_time not in revision.times:
            continue
        for subscription in current:
            selected[subscription.symbol] = subscription
    if not selected:
        logger.info(
            "intraday_schedule_scope_empty",
            category="scheduler",
            scheduled_at=scheduled_at.isoformat(),
        )
        return
    ordered = tuple(selected[symbol] for symbol in sorted(selected))
    await get_realtime_quote_runtime().run(
        symbols=tuple(item.symbol for item in ordered),
        scheduled_at=scheduled_at,
        mode=RealtimeCheckMode.SCHEDULED,
        evaluate_signals=True,
        expected_subscription_versions={
            item.symbol: item.version for item in ordered
        },
        operation_key=f"scheduled:{scheduled_at.isoformat()}",
    )


async def _heartbeat(scheduler: DualPathScheduler, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await scheduler.heartbeat()
        except Exception:
            logger.exception("scheduler_heartbeat_failed", category="scheduler")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)


async def _run_notification_channel(
    runtime: NotificationDeliveryRuntime,
    channel: DeliveryChannel,
    *,
    stop: asyncio.Event,
    poll_seconds: float,
) -> None:
    while not stop.is_set():
        try:
            worked = await runtime.process_once(channel)
        except Exception:
            logger.exception(
                "notification_delivery_cycle_failed",
                category="notifications",
                channel=channel.value,
            )
            worked = False
        if not worked:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


async def run() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    configure_strategy_validation_executor(build_strategy_validation_executor)
    runtime = SchedulerRuntimeApplication(database)
    schedule_application = MonitorScheduleApplication(database)
    subscription_application = MonitorSubscriptionApplication(
        database,
        security_application=SecurityApplication(database),
        schedule_application=schedule_application,
    )
    scheduler = DualPathScheduler(
        calendar=CalendarApplication(database),
        schedules=schedule_application,
        runtime=runtime,
        persistent_submitter=PostgresPersistentJobSubmitter(database),
        intraday_handler=partial(
            _run_intraday,
            schedules=schedule_application,
            subscriptions=subscription_application,
        ),
        instance_id=f"{socket.gethostname()}:{os.getpid()}",
        daily_gap_planner=DailyGapPlanner(
            database, CalendarApplication(database)
        ),
    )
    worker_id = f"daily-market:{socket.gethostname()}:{os.getpid()}"
    runner = PostgresJobRunner(
        database,
        build_market_data_handlers(database),
        worker_id=worker_id,
    )
    history_runner = PostgresJobRunner(
        database,
        {
            "MARKET_HISTORY_BACKFILL": build_history_backfill_job_handler(
                database
            )
        },
        worker_id=f"history-backfill:{socket.gethostname()}:{os.getpid()}",
    )
    notification_runtimes = {
        channel: NotificationDeliveryRuntime(
            database,
            settings,
            worker_id=(
                f"notification-{channel.value.lower()}:"
                f"{socket.gethostname()}:{os.getpid()}"
            ),
        )
        for channel in DeliveryChannel
    }
    listener = PostgresNotificationListener(settings.database_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)

    tasks: list[asyncio.Task[None]] = []
    try:
        await scheduler.start()
        tasks = [
            asyncio.create_task(listener.run(stop, scheduler.refresh)),
            asyncio.create_task(_heartbeat(scheduler, stop)),
            asyncio.create_task(runner.run_forever(stop)),
            asyncio.create_task(history_runner.run_forever(stop)),
            *(
                asyncio.create_task(
                    _run_notification_channel(
                        runtime,
                        channel,
                        stop=stop,
                        poll_seconds=settings.notification_worker_poll_seconds,
                    )
                )
                for channel, runtime in notification_runtimes.items()
            ),
        ]
        await stop.wait()
    finally:
        stop.set()
        await scheduler.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await close_provider_resources()
        await database.dispose()


def main(*, service: str = "longinvest-monitor-scheduler") -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service=service,
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
