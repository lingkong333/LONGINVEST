from fastapi import FastAPI

from long_invest.modules.health.api import router as health_router
from long_invest.platform.config.settings import get_settings
from long_invest.platform.http.request_id import RequestIdMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    return app

