from uuid import uuid4

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, func, insert, select, text
from sqlalchemy.exc import SQLAlchemyError

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.batching import (
    execute_atomic_batches,
    execute_isolated_batches,
)
from long_invest.platform.database.engine import Database


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_postgres_batch_modes_have_distinct_rollback_semantics() -> None:
    settings = AppSettings(_env_file=None)
    database = Database(settings.database_owner_url)
    table_name = f"test_database_batch_{uuid4().hex}"
    test_table = Table(
        table_name,
        MetaData(),
        Column("value", Integer, primary_key=True),
    )

    try:
        async with database.transaction() as session:
            await session.execute(
                text(f'CREATE TABLE "{table_name}" (value integer PRIMARY KEY)')
            )

        with pytest.raises(SQLAlchemyError):
            async with database.transaction() as session:
                await execute_atomic_batches(
                    session,
                    [1, 1, 2],
                    batch_size=1,
                    statement_factory=lambda values: insert(test_table).values(
                        [{"value": value} for value in values]
                    ),
                )

        async with database.session() as session:
            count = await session.scalar(select(func.count()).select_from(test_table))
            assert count == 0

        async with database.transaction() as session:
            result = await execute_isolated_batches(
                session,
                [1, 1, 2],
                batch_size=1,
                statement_factory=lambda values: insert(test_table).values(
                    [{"value": value} for value in values]
                ),
            )

        assert result.attempted == 3
        assert result.succeeded == 2
        assert len(result.failures) == 1
        async with database.session() as session:
            count = await session.scalar(select(func.count()).select_from(test_table))
            assert count == 2
    finally:
        async with database.transaction() as session:
            await session.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        await database.dispose()
