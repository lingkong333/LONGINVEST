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
from long_invest.modules.calendar.api import router as calendar_router
from long_invest.modules.health.api import router as health_router
from long_invest.modules.providers.api import (
    get_provider_service,
)
from long_invest.modules.providers.api import (
    router as providers_router,
)
from long_invest.modules.securities.api import router as securities_router
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
    settings = get_settings()
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
    app.include_router(calendar_router)
    app.include_router(providers_router)
    app.dependency_overrides[get_provider_service] = provide_provider_service
    return app
