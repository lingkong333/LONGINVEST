from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from long_invest.bootstrap import dashboard


@pytest.mark.anyio
async def test_today_signal_events_reads_all_today_pages(monkeypatch) -> None:
    today = datetime.now(UTC)
    pages = {
        1: ([SimpleNamespace(created_at=today)] * 200, 201),
        2: ([SimpleNamespace(created_at=today)], 201),
    }

    class Application:
        async def list_events(self, *, page: int, page_size: int):
            assert page_size == 200
            return pages[page]

    monkeypatch.setattr(dashboard, "get_signal_application", Application)

    assert len(await dashboard._today_signal_events()) == 201


@pytest.mark.anyio
async def test_today_signal_events_stops_at_first_older_event(monkeypatch) -> None:
    today = datetime.now(UTC)
    yesterday = today - timedelta(days=1)
    calls = []

    class Application:
        async def list_events(self, *, page: int, page_size: int):
            calls.append(page)
            return [SimpleNamespace(created_at=today)] * 199 + [
                SimpleNamespace(created_at=yesterday)
            ], 500

    monkeypatch.setattr(dashboard, "get_signal_application", Application)

    assert len(await dashboard._today_signal_events()) == 199
    assert calls == [1]
