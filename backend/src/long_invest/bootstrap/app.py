from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI

from long_invest.modules.auth.api import router as auth_router
from long_invest.modules.auth.application import get_auth_application
from long_invest.modules.health.api import router as health_router
from long_invest.platform.config.settings import get_settings
from long_invest.platform.http.exception_handlers import register_exception_handlers
from long_invest.platform.http.middleware import RequestContextMiddleware
from long_invest.platform.http.request_id import (
    REQUEST_ID_HEADER,
    create_request_id,
    is_valid_request_id,
)


def create_app() -> FastAPI:
    settings = get_settings()
    get_auth_application()
    app = FastAPI(title=settings.app_name)
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
    return app
