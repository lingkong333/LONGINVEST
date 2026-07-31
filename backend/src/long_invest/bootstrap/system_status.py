from long_invest.modules.monitoring.scheduler import (
    get_monitor_occurrence_application,
)
from long_invest.modules.system_status.adapters import (
    ClockStatusAdapter,
    ComponentStatusAdapter,
    PostgresRuntimeStatusAdapter,
    SchedulerStatusAdapter,
)
from long_invest.modules.system_status.application import SystemStatusApplication
from long_invest.modules.system_status.runtime import SchedulerRuntimeApplication
from long_invest.platform.cache.redis import get_redis_probe
from long_invest.platform.database.engine import get_database


def build_system_status_application() -> SystemStatusApplication:
    database = get_database()
    runtime = SchedulerRuntimeApplication(database)
    return SystemStatusApplication(
        components=ComponentStatusAdapter(database, get_redis_probe()),
        runtime=PostgresRuntimeStatusAdapter(database, runtime),
        scheduler=SchedulerStatusAdapter(
            database,
            get_monitor_occurrence_application(),
            runtime,
        ),
        clock=ClockStatusAdapter(database),
    )
