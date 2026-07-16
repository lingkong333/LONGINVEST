from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from long_invest.modules.quotes.outbox import TransactionalQuoteEventAdapter


class Writer:
    def __init__(self):
        self.calls = []

    async def append(self, **kwargs):
        self.calls.append(kwargs)


def cycle(**values):
    defaults = {
        "id": uuid4(),
        "scheduled_at": datetime(2026, 7, 15, tzinfo=UTC),
        "expected_count": 2,
        "universe_snapshot_id": "universe-1",
        "universe_snapshot_version": 3,
        "status": "READY",
    }
    return SimpleNamespace(**(defaults | values))


@pytest.mark.anyio
async def test_finalized_event_contains_only_valid_item_ids_and_stable_dedupe() -> None:
    writer = Writer()
    session = object()
    adapter = TransactionalQuoteEventAdapter(session, writer)
    record = cycle()
    valid = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]
    await adapter.finalized(record, valid)
    call = writer.calls[0]
    assert call["session"] is session
    assert call["topic"] == "quote_cycle.finalized"
    assert call["dedupe_key"] == f"quote-cycle:{record.id}:finalized"
    assert call["payload"] == {
        "event_type": "quote_cycle.finalized",
        "cycle_id": str(record.id),
        "status": "READY",
        "valid_item_ids": [str(item.id) for item in valid],
    }


@pytest.mark.anyio
async def test_created_event_has_exact_tracking_payload_and_stable_dedupe() -> None:
    writer = Writer()
    adapter = TransactionalQuoteEventAdapter(object(), writer)
    record = cycle()
    await adapter.created(record)
    call = writer.calls[0]
    assert call["topic"] == "quote_cycle.created"
    assert call["dedupe_key"] == f"quote-cycle:{record.id}:created"
    assert call["payload"] == {
        "event_type": "quote_cycle.created",
        "cycle_id": str(record.id),
        "scheduled_at": record.scheduled_at.isoformat(),
        "expected_count": 2,
        "universe_snapshot_id": "universe-1",
        "universe_snapshot_version": 3,
    }


@pytest.mark.anyio
async def test_conflict_event_has_exact_reference_payload_and_stable_dedupe() -> None:
    writer = Writer()
    adapter = TransactionalQuoteEventAdapter(object(), writer)
    record = cycle()
    item = SimpleNamespace(id=uuid4(), symbol="600000.SH")
    await adapter.conflict(record, item)
    call = writer.calls[0]
    assert call["topic"] == "quote_conflict.detected"
    assert call["dedupe_key"] == f"quote-item:{item.id}:conflict"
    assert call["payload"] == {
        "event_type": "quote_conflict.detected",
        "cycle_id": str(record.id),
        "item_id": str(item.id),
        "symbol": "600000.SH",
    }


@pytest.mark.anyio
async def test_missing_event_is_one_batch_aggregate_with_stable_dedupe() -> None:
    writer = Writer()
    adapter = TransactionalQuoteEventAdapter(object(), writer)
    record = cycle()
    item = SimpleNamespace(symbol="600000.SH", error_code="QUOTE_STALE", status="STALE")
    await adapter.missing(record, [item])
    call = writer.calls[0]
    assert call["topic"] == "quote_item.missing"
    assert call["dedupe_key"] == f"quote-cycle:{record.id}:missing"
    assert call["payload"]["items"] == [
        {"symbol": "600000.SH", "error_code": "QUOTE_STALE", "status": "STALE"}
    ]


@pytest.mark.anyio
async def test_writer_failure_propagates_without_committing_session() -> None:
    class FailingWriter:
        async def append(self, **_kwargs):
            raise RuntimeError("outbox failed")

    session = SimpleNamespace(commit=AsyncMock())
    adapter = TransactionalQuoteEventAdapter(session, FailingWriter())
    with pytest.raises(RuntimeError, match="outbox failed"):
        await adapter.created(cycle())
    session.commit.assert_not_awaited()
