from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from long_invest.modules.calendar.contracts import CalendarDayStatus
from long_invest.modules.calendar.repository import (
    CalendarRepository,
    _continuous_confirmed_through,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_repository_provides_all_calendar_read_paths() -> None:
    current = MagicMock(version_id=uuid4(), pointer_version=3)
    day = MagicMock(
        trade_date=date(2026, 7, 15),
        status=CalendarDayStatus.CONFIRMED,
    )
    version = MagicMock(id=current.version_id)
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[current, day, day, day, version])
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
    ) == date(2026, 7, 15)
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


@pytest.mark.anyio
async def test_repository_reads_version_and_dates_in_one_query() -> None:
    version_id = uuid4()
    result = MagicMock()
    result.all.return_value = [
        (version_id, 4, date(2026, 7, 15)),
        (version_id, 4, date(2026, 7, 16)),
    ]
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    window = await CalendarRepository(session).trading_date_window(
        "CN_A", date(2026, 7, 14), date(2026, 7, 16)
    )

    assert window.version_id == version_id
    assert window.version_number == 4
    assert window.dates == (date(2026, 7, 15), date(2026, 7, 16))
    session.execute.assert_awaited_once()
    query = session.execute.await_args.args[0]
    statement = str(query)
    assert "trading_calendar_day.is_trading_day IS true" in statement
    assert "trading_calendar_day.status IN" in statement
    assert "ORDER BY trading_calendar_day.trade_date" in statement
    assert set(query.compile().params["status_1"]) == {
        CalendarDayStatus.CONFIRMED,
        CalendarDayStatus.OVERRIDDEN,
    }


@pytest.mark.anyio
async def test_repository_preserves_current_version_when_no_days_are_eligible() -> None:
    version_id = uuid4()
    result = MagicMock()
    result.all.return_value = [(version_id, 4, None)]
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    window = await CalendarRepository(session).trading_date_window(
        "CN_A", date(2026, 7, 18), date(2026, 7, 19)
    )

    assert window.version_id == version_id
    assert window.dates == ()


def test_confirmed_through_stops_at_first_natural_day_gap_or_unconfirmed_day() -> None:
    rows = [
        MagicMock(
            trade_date=date(2026, 7, 15),
            status=CalendarDayStatus.CONFIRMED,
        ),
        MagicMock(
            trade_date=date(2026, 7, 16),
            status=CalendarDayStatus.OVERRIDDEN,
        ),
        MagicMock(
            trade_date=date(2026, 7, 18),
            status=CalendarDayStatus.CONFIRMED,
        ),
    ]

    assert _continuous_confirmed_through(rows, date(2026, 7, 15)) == date(
        2026, 7, 16
    )
