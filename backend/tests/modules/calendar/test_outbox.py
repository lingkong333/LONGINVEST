from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.modules.calendar.contracts import CalendarEvent
from long_invest.modules.calendar.outbox import CalendarOutboxAdapter
from long_invest.platform.errors import AppError
from long_invest.platform.outbox.models import EventOutbox


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_calendar_outbox_adapter_writes_reliable_event_on_session() -> None:
    expected = MagicMock(spec=EventOutbox)
    session = MagicMock()
    session.scalar = AsyncMock(return_value=expected)
    adapter = CalendarOutboxAdapter(session)
    event = CalendarEvent(
        event_type="trading_calendar.updated",
        aggregate_id="3e39d8f5-4e40-4508-a5f5-d2f2931c14e8",
        idempotency_key="calendar-import-1",
        payload={"request_id": "req-1"},
    )

    stored = await adapter.append(event)

    assert stored is expected
    statement = session.scalar.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("INSERT INTO event_outbox")
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in sql
    assert "RETURNING event_outbox.id" in sql
    assert compiled.params["topic"] == "trading_calendar.updated"
    assert compiled.params["aggregate_type"] == "trading_calendar"
    assert compiled.params["payload"]["request_id"] == "req-1"


@pytest.mark.anyio
async def test_calendar_outbox_adapter_reuses_existing_deduplicated_event() -> None:
    existing = MagicMock(spec=EventOutbox)
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[None, existing])
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
    insert_statement = session.scalar.await_args_list[0].args[0]
    select_statement = session.scalar.await_args_list[1].args[0]
    assert str(insert_statement).startswith("INSERT INTO event_outbox")
    assert str(select_statement).startswith("SELECT event_outbox.id")
    session.add.assert_not_called()


@pytest.mark.anyio
async def test_unresolved_conflict_visibility_returns_stable_503() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[None, None])
    adapter = CalendarOutboxAdapter(session)

    with pytest.raises(AppError) as caught:
        await adapter.append(
            CalendarEvent(
                event_type="trading_calendar.updated",
                aggregate_id="CN_A",
                idempotency_key="concurrent-event",
                payload={},
            )
        )

    assert caught.value.code == "CALENDAR_OUTBOX_CONFLICT_UNRESOLVED"
    assert caught.value.status_code == 503
