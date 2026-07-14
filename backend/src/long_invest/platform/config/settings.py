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
    database_url: str = (
        "postgresql+asyncpg://longinvest:longinvest@postgres:5432/longinvest"
    )
    redis_url: str = "redis://redis:6379/0"


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
