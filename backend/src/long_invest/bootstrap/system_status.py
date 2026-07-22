from long_invest.modules.monitoring.scheduler import (
    get_monitor_occurrence_application,
)
from long_invest.modules.system_status.adapters import (
    ClockStatusAdapter,
    ComponentStatusAdapter,
    RqRuntimeStatusAdapter,
    SchedulerStatusAdapter,
)
from long_invest.modules.system_status.application import SystemStatusApplication
from long_invest.platform.cache.redis import get_redis_probe
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import get_database


def build_system_status_application() -> SystemStatusApplication:
    settings = get_settings()
    database = get_database()
    return SystemStatusApplication(
        components=ComponentStatusAdapter(database, get_redis_probe()),
        runtime=RqRuntimeStatusAdapter(settings.redis_url),
        scheduler=SchedulerStatusAdapter(
            database, get_monitor_occurrence_application()
        ),
        clock=ClockStatusAdapter(database),
    )
