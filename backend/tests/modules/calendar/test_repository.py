from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from long_invest.modules.calendar.repository import CalendarRepository


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_repository_provides_all_calendar_read_paths() -> None:
    current = MagicMock(version_id=uuid4(), pointer_version=3)
    day = MagicMock(trade_date=date(2026, 7, 15))
    version = MagicMock(id=current.version_id)
    session = MagicMock()
    session.scalar = AsyncMock(
        side_effect=[current, day, day, day, version, date(2026, 9, 30)]
    )
    scalars_result = MagicMock()
    scalars_result.all.return_value = [day]
    session.scalars = AsyncMock(return_value=scalars_result)
    repository = CalendarRepository(session)

    assert await repository.get_current("CN_A") is current
    assert await repository.get_day("CN_A", date(2026, 7, 15)) is day
    assert await repository.next_trading_day("CN_A", date(2026, 7, 14)) is day
    assert await repository.previous_trading_day("CN_A", date(2026, 7, 16)) is day
    assert await repository.get_version(version.id) is version
    assert await repository.confirmed_through(
        "CN_A", date(2026, 7, 15)
    ) == date(2026, 9, 30)
    assert await repository.list_days(
        "CN_A", date(2026, 7, 1), date(2026, 7, 31)
    ) == [day]


@pytest.mark.anyio
async def test_repository_persists_and_atomically_switches_current_pointer() -> None:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.scalar = AsyncMock(return_value=uuid4())
    repository = CalendarRepository(session)
    version = MagicMock()

    await repository.add_version(version)
    switched = await repository.switch_current(
        market="CN_A",
        version_id=uuid4(),
        expected_pointer_version=2,
    )

    assert switched is True
    session.add.assert_called_once_with(version)
    assert session.flush.await_count == 1
