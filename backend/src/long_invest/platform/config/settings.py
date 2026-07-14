from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LONGINVEST_",
        extra="ignore",
    )

    app_name: str = "LongInvest"
    environment: Literal["development", "test", "production"] = "development"
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_queue_capacity: int = Field(default=10_000, ge=100, le=100_000)
    log_file: str | None = None
    database_url: str = (
        "postgresql+asyncpg://longinvest_app:longinvest-app-local-only"
        "@postgres:5432/longinvest"
    )
    database_owner_url: str = (
        "postgresql+asyncpg://longinvest:longinvest-local-only"
        "@postgres:5432/longinvest"
    )
    database_app_role: str = "longinvest_app"
    database_app_password: str = "longinvest-app-local-only"
    redis_url: str = "redis://redis:6379/0"


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
