from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.migrations import expected_database_revisions


class Database:
    def __init__(self, url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    async def ping(self) -> bool:
        async with self._engine.connect() as connection:
            return await connection.scalar(text("SELECT 1")) == 1

    async def migration_is_current(self) -> bool:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text("SELECT version_num FROM alembic_version")
            )
            current = frozenset(result.scalars())
        return current == expected_database_revisions()

    async def dispose(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session, session.begin():
            yield session


@lru_cache
def get_database() -> Database:
    return Database(get_settings().database_url)
