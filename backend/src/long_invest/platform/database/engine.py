from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from long_invest.platform.config.settings import get_settings


class Database:
    def __init__(self, url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
        )

    async def ping(self) -> bool:
        async with self._engine.connect() as connection:
            return await connection.scalar(text("SELECT 1")) == 1

    async def dispose(self) -> None:
        await self._engine.dispose()


@lru_cache
def get_database() -> Database:
    return Database(get_settings().database_url)

