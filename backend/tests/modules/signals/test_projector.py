from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.modules.signals.projector import (
    SUPPORTED_SIGNAL_EVENT_TOPICS,
    SignalEventProjector,
    SignalProjectionEvent,
    SignalProjectionRepository,
)

NOW = datetime(2026, 7, 21, 8, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self, database) -> None:
        self.database = database


class FakeDatabase:
    def __init__(self) -> None:
        self.state = {"events": [], "jobs": []}
        self.session = FakeSession(self)
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        before = deepcopy(self.state)
        try:
            yield self.session
        except Exception:
            self.state = before
            self.rolled_back = True
            raise


class FakeProjectionRepository:
    fail_mark = False

    def __init__(self, session) -> None:
        self.session = session

    async def claim_supported(self, *, limit):
        return tuple(
            SignalProjectionEvent(
                id=event["id"],
                topic=event["topic"],
                aggregate_id=event["aggregate_id"],
                payload=event["payload"],
            )
            for event in self.session.database.state["events"]
            if event["committed"]
            and event["status"] == "PENDING"
            and event["topic"] in SUPPORTED_SIGNAL_EVENT_TOPICS
        )[:limit]

    async def mark_dispatched(self, event_id, *, dispatched_at):
        if type(self).fail_mark:
            raise RuntimeError("mark unavailable")
        event = next(
            item
            for item in self.session.database.state["events"]
            if item["id"] == event_id
        )
        event["status"] = "DISPATCHED"
        event["dispatched_at"] = dispatched_at


class FakeJobService:
    error = None

    def __init__(self, session) -> None:
        self.session = session

    async def submit(self, command):
        if type(self).error is not None:
            raise type(self).error
        jobs = self.session.database.state["jobs"]
        existing = next(
            (
                item
                for item in jobs
                if item.idempotency_scope == command.idempotency_scope
                and item.idempotency_key == command.idempotency_key
            ),
            None,
        )
        if existing is None:
            jobs.append(command)
        return SimpleNamespace(id=uuid4(), job_type=command.job_type)


@pytest.fixture(autouse=True)
def reset_fakes() -> None:
    FakeJobService.error = None
    FakeProjectionRepository.fail_mark = False


def add_event(
    database,
    topic,
    payload,
    *,
    event_id=None,
    committed=True,
):
    event_id = event_id or uuid4()
    database.state["events"].append(
        {
            "id": event_id,
            "topic": topic,
            "aggregate_id": str(uuid4()),
            "payload": payload,
            "status": "PENDING",
            "committed": committed,
            "dispatched_at": None,
        }
    )
    return event_id


def projector(database):
    return SignalEventProjector(
        database,
        repository_factory=FakeProjectionRepository,
        job_service_factory=FakeJobService,
        clock=lambda: NOW,
    )


@pytest.mark.anyio
async def test_finalized_quote_event_creates_one_frozen_batch_job() -> None:
    database = FakeDatabase()
    event_id = add_event(
        database,
        "quote_cycle.finalized",
        {
            "cycle_id": "cycle-1",
            "valid_item_ids": ["item-1", "item-2"],
        },
    )

    report = await projector(database).project_once()

    assert report.projected == 1
    assert report.claimed == 1
    command = database.state["jobs"][0]
    assert command.job_type == "SIGNAL_EVALUATE_BATCH"
    assert command.queue == "signals"
    assert command.idempotency_scope == "signal-event-projector"
    assert command.idempotency_key == f"quote_cycle.finalized:{event_id}"
    assert command.config_snapshot == {
        "source_event_id": str(event_id),
        "reason": "QUOTE_FINALIZED",
        "quote_cycle_id": "cycle-1",
        "eligible_item_ids": ["item-1", "item-2"],
    }
    assert database.state["events"][0]["status"] == "DISPATCHED"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("topic", "payload", "expected"),
    [
        (
            "target.activated",
            {
                "subscription_id": "subscription-1",
                "revision_id": "revision-3",
                "binding_version": 4,
                "request_id": "req-target",
            },
            {
                "reason": "TARGET_ACTIVATED",
                "subscription_id": "subscription-1",
                "target_revision_id": "revision-3",
                "target_binding_version": 4,
            },
        ),
        (
            "position.became_holding",
            {
                "security_id": "security-1",
                "symbol": "600000.SH",
                "position_version": 6,
                "request_id": "req-position",
            },
            {
                "reason": "POSITION_BECAME_HOLDING",
                "security_id": "security-1",
                "symbol": "600000.SH",
                "position_version": 6,
            },
        ),
    ],
)
async def test_change_event_creates_payload_only_reevaluation_job(
    topic,
    payload,
    expected,
) -> None:
    database = FakeDatabase()
    event_id = add_event(database, topic, payload)

    await projector(database).project_once()

    command = database.state["jobs"][0]
    assert command.job_type == "SIGNAL_REEVALUATE"
    assert command.request_id == payload["request_id"]
    assert command.config_snapshot == {
        "source_event_id": str(event_id),
        **expected,
    }


@pytest.mark.anyio
async def test_uncommitted_and_unsupported_events_are_not_projected() -> None:
    database = FakeDatabase()
    add_event(
        database,
        "quote_cycle.finalized",
        {"cycle_id": "cycle-1", "valid_item_ids": ["item-1"]},
        committed=False,
    )
    add_event(database, "quote_cycle.created", {"cycle_id": "cycle-2"})

    report = await projector(database).project_once()

    assert report.claimed == 0
    assert report.projected == 0
    assert database.state["jobs"] == []
    assert all(event["status"] == "PENDING" for event in database.state["events"])


@pytest.mark.anyio
async def test_projector_retry_does_not_duplicate_job() -> None:
    database = FakeDatabase()
    event_id = add_event(
        database,
        "quote_cycle.finalized",
        {"cycle_id": "cycle-1", "valid_item_ids": ["item-1"]},
    )

    first = await projector(database).project_once()
    database.state["events"][0]["status"] = "PENDING"
    second = await projector(database).project_once()

    assert first.projected == second.projected == 1
    assert len(database.state["jobs"]) == 1
    assert database.state["jobs"][0].idempotency_key == (
        f"quote_cycle.finalized:{event_id}"
    )


@pytest.mark.anyio
async def test_job_failure_rolls_back_source_event_and_job() -> None:
    database = FakeDatabase()
    add_event(
        database,
        "quote_cycle.finalized",
        {"cycle_id": "cycle-1", "valid_item_ids": ["item-1"]},
    )
    FakeJobService.error = RuntimeError("job unavailable")

    with pytest.raises(RuntimeError, match="job unavailable"):
        await projector(database).project_once()

    assert database.rolled_back is True
    assert database.state["events"][0]["status"] == "PENDING"
    assert database.state["jobs"] == []


@pytest.mark.anyio
async def test_mark_failure_rolls_back_already_created_job() -> None:
    database = FakeDatabase()
    add_event(
        database,
        "quote_cycle.finalized",
        {"cycle_id": "cycle-1", "valid_item_ids": ["item-1"]},
    )
    FakeProjectionRepository.fail_mark = True

    with pytest.raises(RuntimeError, match="mark unavailable"):
        await projector(database).project_once()

    assert database.rolled_back is True
    assert database.state["events"][0]["status"] == "PENDING"
    assert database.state["jobs"] == []


@pytest.mark.anyio
async def test_invalid_event_payload_is_left_for_retry() -> None:
    database = FakeDatabase()
    add_event(
        database,
        "quote_cycle.finalized",
        {"cycle_id": "cycle-1", "valid_item_ids": "item-1"},
    )

    with pytest.raises(ValueError, match="valid_item_ids"):
        await projector(database).project_once()

    assert database.state["events"][0]["status"] == "PENDING"
    assert database.state["jobs"] == []


def test_repository_claim_uses_supported_pending_skip_locked_query() -> None:
    statement = SignalProjectionRepository.claim_statement(limit=25, now=NOW)
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "quote_cycle.finalized" in sql
    assert "target.activated" in sql
    assert "position.became_holding" in sql
    assert "event_outbox.status = 'PENDING'" in sql
    assert "event_outbox.next_attempt_at <=" in sql
    assert "ORDER BY event_outbox.created_at, event_outbox.id" in sql
    assert "LIMIT 25 FOR UPDATE SKIP LOCKED" in sql


def test_projection_event_freezes_payload() -> None:
    payload = {"valid_item_ids": ["item-1"]}
    event = SignalProjectionEvent(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        topic="quote_cycle.finalized",
        aggregate_id="cycle-1",
        payload=payload,
    )

    payload["valid_item_ids"].append("item-2")

    assert event.payload == {"valid_item_ids": ("item-1",)}
