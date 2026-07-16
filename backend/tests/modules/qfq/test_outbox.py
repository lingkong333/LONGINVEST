from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.qfq.outbox import QfqEventAdapter


class Writer:
    def __init__(self):
        self.calls = []

    async def append(self, **kwargs):
        self.calls.append(kwargs)


def _run():
    return SimpleNamespace(
        id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        requested_start=date(2026, 7, 15),
        requested_end=date(2026, 7, 16),
        as_of_date=date(2026, 7, 16),
        input_daily_version=3,
        trigger_reason="MANUAL",
        error_code="QFQ_PROVIDER_FAILED",
        retryable=True,
    )


@pytest.mark.anyio
async def test_completed_event_has_stable_payload_and_dedupe_key() -> None:
    writer = Writer()
    session = object()
    adapter = QfqEventAdapter(session, writer)
    run = _run()
    dataset = SimpleNamespace(id=uuid4(), version=2, row_count=2, checksum="a" * 64)

    await adapter.completed(run, dataset)

    call = writer.calls[0]
    assert call["session"] is session
    assert call["topic"] == "qfq_refresh.completed"
    assert call["dedupe_key"] == f"qfq:{run.id}:completed"
    assert call["payload"] == {
        "event_type": "qfq_refresh.completed",
        "run_id": str(run.id),
        "security_id": str(run.security_id),
        "symbol": "600000.SH",
        "dataset_id": str(dataset.id),
        "version": 2,
        "start": "2026-07-15",
        "end": "2026-07-16",
        "as_of_date": "2026-07-16",
        "row_count": 2,
        "checksum": "a" * 64,
        "input_daily_version": 3,
        "trigger_reason": "MANUAL",
    }


@pytest.mark.anyio
async def test_failed_event_reports_old_data_without_provider_payload() -> None:
    writer = Writer()
    adapter = QfqEventAdapter(object(), writer)
    run = _run()
    current = SimpleNamespace(id=uuid4(), freshness="STALE")

    await adapter.failed(run, current)

    call = writer.calls[0]
    assert call["topic"] == "qfq_refresh.failed"
    assert call["dedupe_key"] == f"qfq:{run.id}:failed"
    assert call["payload"] == {
        "event_type": "qfq_refresh.failed",
        "run_id": str(run.id),
        "security_id": str(run.security_id),
        "symbol": "600000.SH",
        "start": "2026-07-15",
        "end": "2026-07-16",
        "as_of_date": "2026-07-16",
        "error_code": "QFQ_PROVIDER_FAILED",
        "has_current_dataset": True,
        "current_dataset_stale": True,
        "retryable": True,
        "trigger_reason": "MANUAL",
    }
    assert "provider_payload" not in call["payload"]
