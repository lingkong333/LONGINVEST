from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.calendar.application import CalendarApplication
from long_invest.platform.errors import AppError


class FakeDatabase:
    @asynccontextmanager
    async def session(self):
        yield object()


class FakeRepository:
    result = None
    error = None
    calls = []

    def __init__(self, session) -> None:
        self.session = session

    async def trading_date_window(self, market, start, end):
        type(self).calls.append((self.session, market, start, end))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def reset_repository() -> None:
    FakeRepository.result = None
    FakeRepository.error = None
    FakeRepository.calls = []


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def application() -> CalendarApplication:
    return CalendarApplication(FakeDatabase(), repository_factory=FakeRepository)


@pytest.mark.anyio
async def test_trading_dates_returns_version_and_ordered_official_days() -> None:
    version_id = uuid4()
    FakeRepository.result = SimpleNamespace(
        version_id=version_id,
        version_number=7,
        dates=(date(2026, 7, 15), date(2026, 7, 16)),
    )

    result = await application().trading_dates(date(2026, 7, 14), date(2026, 7, 16))

    assert result.model_dump() == {
        "market": "CN_A",
        "start": date(2026, 7, 14),
        "end": date(2026, 7, 16),
        "version_id": version_id,
        "version_number": 7,
        "dates": (date(2026, 7, 15), date(2026, 7, 16)),
    }
    assert FakeRepository.calls == [
        (FakeRepository.calls[0][0], "CN_A", date(2026, 7, 14), date(2026, 7, 16))
    ]


@pytest.mark.anyio
async def test_trading_dates_preserves_current_version_for_empty_window() -> None:
    FakeRepository.result = SimpleNamespace(
        version_id=uuid4(), version_number=3, dates=()
    )

    result = await application().trading_dates(date(2026, 7, 18), date(2026, 7, 19))

    assert result.version_number == 3
    assert result.dates == ()


@pytest.mark.anyio
async def test_trading_dates_rejects_inverted_window_before_database_access() -> None:
    with pytest.raises(AppError) as caught:
        await application().trading_dates(date(2026, 7, 17), date(2026, 7, 16))

    assert caught.value.code == "CALENDAR_DATE_RANGE_INVALID"
    assert caught.value.status_code == 422
    assert FakeRepository.calls == []


@pytest.mark.anyio
async def test_trading_dates_requires_a_current_calendar() -> None:
    with pytest.raises(AppError) as caught:
        await application().trading_dates(date(2026, 7, 15), date(2026, 7, 16))

    assert caught.value.code == "CALENDAR_CURRENT_NOT_FOUND"
    assert caught.value.status_code == 404


@pytest.mark.anyio
async def test_trading_dates_maps_database_failures_to_stable_error() -> None:
    FakeRepository.error = SQLAlchemyError("database unavailable")

    with pytest.raises(AppError) as caught:
        await application().trading_dates(date(2026, 7, 15), date(2026, 7, 16))

    assert caught.value.code == "CALENDAR_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503
