import pytest

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_database_ping_uses_real_postgresql() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        assert await database.ping() is True
    finally:
        await database.dispose()

