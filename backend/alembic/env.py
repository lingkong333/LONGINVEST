import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from long_invest.modules.auth.models import AppUser, UserSession  # noqa: F401
from long_invest.modules.backtests.models import (  # noqa: F401
    BacktestControlCommand,
    BacktestDailyResult,
    BacktestForecastSnapshot,
    BacktestItem,
    BacktestMetric,
    BacktestOrder,
    BacktestTargetAdjustment,
    BacktestTask,
    BacktestTrade,
    BacktestUniverseSnapshot,
)
from long_invest.modules.calendar.models import (  # noqa: F401
    TradingCalendarCurrent,
    TradingCalendarDay,
    TradingCalendarVersion,
    TradingSession,
)
from long_invest.modules.daily_data.models import (  # noqa: F401
    DailyBarRevision,
    DailyBarStage,
    DailyBarUnadjusted,
    DailyBatchMissingItem,
    DailyDataBatch,
)
from long_invest.modules.market_data.models import DataQualityIssue  # noqa: F401
from long_invest.modules.monitor_schedules.models import (  # noqa: F401
    MonitorSchedule,
    MonitorScheduleRevision,
)
from long_invest.modules.monitoring.models import (  # noqa: F401
    MonitorSubscription,
    MonitorSubscriptionRevision,
    ScheduleOccurrence,
)
from long_invest.modules.notifications.models import (  # noqa: F401
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationEvent,
)
from long_invest.modules.positions.models import (  # noqa: F401
    UserPosition,
    UserPositionHistory,
)
from long_invest.modules.providers.models import (  # noqa: F401
    ProviderCapabilitySetting,
    ProviderCircuitHistory,
    ProviderCircuitState,
    ProviderConfigVersion,
    ProviderFailureSample,
    ProviderHealthState,
    ProviderMutationRequest,
)
from long_invest.modules.qfq.models import (  # noqa: F401
    QfqDataset,
    QfqDatasetBar,
    QfqRefreshRun,
)
from long_invest.modules.quotes.models import QuoteCycle, QuoteCycleItem  # noqa: F401
from long_invest.modules.securities.models import (  # noqa: F401
    Security,
    SecurityMasterVersion,
    SecurityRevision,
    SecurityUniverseSnapshot,
    SecurityUniverseSnapshotItem,
)
from long_invest.modules.signals.models import (  # noqa: F401
    SignalEvaluation,
    SignalEvent,
    SignalState,
)
from long_invest.modules.strategies.models import (  # noqa: F401
    Strategy,
    StrategyDraft,
    StrategyDraftRevision,
    StrategyRun,
    StrategyValidationRun,
    StrategyVersion,
)
from long_invest.modules.targets.models import (  # noqa: F401
    SubscriptionTargetBinding,
    TargetCalculationRun,
    TargetReview,
    TargetRevision,
)
from long_invest.modules.watchlists.models import Watchlist, WatchlistItem  # noqa: F401
from long_invest.platform.audit.models import AuditEvent  # noqa: F401
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.base import Base
from long_invest.platform.jobs.models import Job, JobItem, JobRun  # noqa: F401
from long_invest.platform.outbox.models import EventOutbox  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_owner_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
