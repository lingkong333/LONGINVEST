from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI

from long_invest.bootstrap.providers import (
    close_provider_resources,
    get_provider_resources,
    provide_provider_service,
)
from long_invest.modules.auth.api import router as auth_router
from long_invest.modules.auth.application import (
    close_auth_resources,
    get_auth_application,
)
from long_invest.modules.backtests.api import (
    configure_backtest_application,
)
from long_invest.modules.backtests.api import (
    router as backtests_router,
)
from long_invest.modules.calendar.api import router as calendar_router
from long_invest.modules.daily_data.api import router as daily_data_router
from long_invest.modules.health.api import router as health_router
from long_invest.modules.monitor_schedules.api import router as monitor_schedules_router
from long_invest.modules.monitoring.api import router as monitoring_router
from long_invest.modules.notifications.api import router as notifications_router
from long_invest.modules.positions.api import router as positions_router
from long_invest.modules.providers.api import (
    get_provider_service,
)
from long_invest.modules.providers.api import (
    router as providers_router,
)
from long_invest.modules.qfq.api import router as qfq_router
from long_invest.modules.quotes.api import router as quotes_router
from long_invest.modules.securities.api import router as securities_router
from long_invest.modules.settings.api import router as settings_router
from long_invest.modules.signals.api import router as signals_router
from long_invest.modules.strategies.api import router as strategies_router
from long_invest.modules.targets.api import router as targets_router
from long_invest.modules.watchlists.api import router as watchlists_router
from long_invest.platform.config.settings import get_settings
from long_invest.platform.http.exception_handlers import register_exception_handlers
from long_invest.platform.http.middleware import RequestContextMiddleware
from long_invest.platform.http.request_id import (
    REQUEST_ID_HEADER,
    create_request_id,
    is_valid_request_id,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_auth_application()
    get_provider_resources()
    yield
    await close_provider_resources()
    await close_auth_resources()


def create_app() -> FastAPI:
    from long_invest.bootstrap.stage4_runtime import build_backtest_application

    settings = get_settings()
    configure_backtest_application(build_backtest_application)
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CorrelationIdMiddleware,
        header_name=REQUEST_ID_HEADER,
        update_request_header=True,
        generator=create_request_id,
        validator=is_valid_request_id,
    )
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(securities_router)
    app.include_router(settings_router)
    app.include_router(calendar_router)
    app.include_router(providers_router)
    app.include_router(quotes_router)
    app.include_router(daily_data_router)
    app.include_router(qfq_router)
    app.include_router(watchlists_router)
    app.include_router(monitor_schedules_router)
    app.include_router(positions_router)
    app.include_router(monitoring_router)
    app.include_router(notifications_router)
    app.include_router(targets_router)
    app.include_router(signals_router)
    app.include_router(strategies_router)
    app.include_router(backtests_router)
    app.dependency_overrides[get_provider_service] = provide_provider_service
    return app
