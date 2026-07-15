from unittest.mock import AsyncMock, MagicMock

import pytest

from long_invest.modules.calendar.contracts import CalendarEvent
from long_invest.modules.calendar.outbox import CalendarOutboxAdapter
from long_invest.platform.outbox.models import EventOutbox


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_calendar_outbox_adapter_writes_reliable_event_on_session() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    adapter = CalendarOutboxAdapter(session)
    event = CalendarEvent(
        event_type="trading_calendar.updated",
        aggregate_id="3e39d8f5-4e40-4508-a5f5-d2f2931c14e8",
        idempotency_key="calendar-import-1",
        payload={"request_id": "req-1"},
    )

    stored = await adapter.append(event)

    assert isinstance(stored, EventOutbox)
    assert stored.topic == "trading_calendar.updated"
    assert stored.aggregate_type == "trading_calendar"
    assert stored.payload["request_id"] == "req-1"
    assert stored.dedupe_key.startswith("calendar:")
    session.add.assert_called_once_with(stored)
    session.flush.assert_awaited_once()


@pytest.mark.anyio
async def test_calendar_outbox_adapter_reuses_existing_deduplicated_event() -> None:
    existing = MagicMock(spec=EventOutbox)
    session = MagicMock()
    session.scalar = AsyncMock(return_value=existing)
    adapter = CalendarOutboxAdapter(session)

    result = await adapter.append(
        CalendarEvent(
            event_type="trading_calendar.missing",
            aggregate_id="CN_A",
            idempotency_key="missing-2026-07-15",
            payload={},
        )
    )

    assert result is existing
    session.add.assert_not_called()
