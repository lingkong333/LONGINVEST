from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.quotes.application import (
    QuoteApplication,
    TransactionalQuoteSignalPort,
)
from long_invest.modules.quotes.collection import DEFAULT_CLEANUP_TIMEOUT_SECONDS
from long_invest.modules.quotes.contracts import QuoteItemStatus, SignalQuoteSnapshot


class Transaction:
    def __init__(self):
        self.session = object()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class Database:
    def __init__(self):
        self.transactions = []

    def transaction(self):
        value = Transaction()
        self.transactions.append(value)
        return value


class Jobs:
    commands = []
    sessions = []
    stored = {}

    def __init__(self, session):
        self.session = session
        self.sessions.append(session)

    async def lock_submission(self, scope, key):
        return None

    async def find_submission(self, scope, key):
        return self.stored.get((scope, key))

    async def submit(self, command):
        self.commands.append(command)
        key = (command.idempotency_scope, command.idempotency_key)
        job = self.stored.get(key)
        if job is None:
            job = SimpleNamespace(
                id=f"job-{len(self.stored) + 1}",
                status="PENDING_DISPATCH",
                config_snapshot=command.config_snapshot,
            )
            self.stored[key] = job
        return job


class UniverseFreezer:
    def __init__(self):
        self.scopes = []

    async def __call__(self, symbols):
        self.scopes.append(symbols)
        return SimpleNamespace(id="snapshot-1", master_version=7)


@pytest.mark.anyio
async def test_manual_and_diagnostic_use_distinct_idempotent_job_types() -> None:
    Jobs.commands = []
    Jobs.sessions = []
    Jobs.stored = {}
    database = Database()
    freezer = UniverseFreezer()
    app = QuoteApplication(
        database,
        job_service_factory=Jobs,
        universe_freezer=freezer,
    )
    common = {
        "symbols": ("600000.SH",),
        "idempotency_key": "same",
        "request_id": "req-1",
        "created_by_user_id": "user-1",
    }
    await app.submit_manual(timeout_seconds=30, **common)
    await app.submit_diagnostic(
        session_id="session-1",
        trusted_ip="127.0.0.1",
        **common,
    )
    assert [command.job_type for command in Jobs.commands] == [
        "REALTIME_QUOTE_CYCLE",
        "QUOTE_DIAGNOSTIC",
    ]
    assert Jobs.commands[0].config_snapshot["symbols"] == ["600000.SH"]
    assert Jobs.commands[0].config_snapshot["universe_snapshot_id"] == "snapshot-1"
    assert Jobs.commands[0].config_snapshot["universe_snapshot_version"] == 7
    assert Jobs.commands[0].soft_timeout_seconds == 35
    assert Jobs.commands[0].hard_timeout_seconds == 45
    assert (
        Jobs.commands[0].hard_timeout_seconds
        - Jobs.commands[0].soft_timeout_seconds
        > DEFAULT_CLEANUP_TIMEOUT_SECONDS
    )
    assert freezer.scopes == [("600000.SH",), ("600000.SH",)]
    assert Jobs.commands[0].idempotency_scope != Jobs.commands[1].idempotency_scope
    assert Jobs.commands[1].config_snapshot["audit"] == {
        "request_id": "req-1",
        "idempotency_key": "same",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
        "reason": "manual quote diagnostic",
    }
    assert Jobs.sessions == [
        transaction.session for transaction in database.transactions
    ]


@pytest.mark.anyio
async def test_idempotent_replay_reuses_the_original_universe_snapshot() -> None:
    Jobs.commands = []
    Jobs.sessions = []
    Jobs.stored = {}
    freezer = UniverseFreezer()
    app = QuoteApplication(
        Database(), job_service_factory=Jobs, universe_freezer=freezer
    )
    values = {
        "symbols": ("600000.SH",),
        "timeout_seconds": 60,
        "idempotency_key": "same",
        "request_id": "request-1",
        "created_by_user_id": "user-1",
    }
    first = await app.submit_manual(**values)
    values["request_id"] = "request-2"
    replay = await app.submit_manual(**values)

    assert replay is first
    assert freezer.scopes == [("600000.SH",)]
    assert Jobs.commands[-1].config_snapshot == Jobs.commands[0].config_snapshot
    assert Jobs.commands[-1].soft_timeout_seconds == 65
    assert Jobs.commands[-1].hard_timeout_seconds == 75


@pytest.mark.anyio
async def test_transactional_signal_port_maps_snapshot_without_committing() -> None:
    cycle_id = uuid4()
    item_id = uuid4()
    scheduled_at = datetime(2026, 7, 17, 1, 30, tzinfo=UTC)
    quote_time = datetime(2026, 7, 17, 1, 31, tzinfo=UTC)

    class Session:
        async def commit(self):
            raise AssertionError("caller owns the transaction")

    session = Session()

    class Repository:
        def __init__(self, received_session):
            assert received_session is session

        async def get_signal_item(self, *, item_id, cycle_id):
            return SimpleNamespace(
                id=item_id,
                cycle_id=cycle_id,
                symbol="600000.SH",
                status=QuoteItemStatus.VALID,
                price=Decimal("10.250000"),
                quote_time=quote_time,
                eligible_for_evaluation=True,
                expected_subscription_version=8,
                cycle=SimpleNamespace(scheduled_at=scheduled_at),
            )

    snapshot = await TransactionalQuoteSignalPort(
        session,
        repository_factory=Repository,
    ).get_quote_snapshot(item_id=item_id, cycle_id=cycle_id)

    assert snapshot == SignalQuoteSnapshot(
        cycle_id=cycle_id,
        item_id=item_id,
        symbol="600000.SH",
        status=QuoteItemStatus.VALID,
        price=Decimal("10.250000"),
        quote_time=quote_time,
        scheduled_at=scheduled_at,
        eligible_for_evaluation=True,
        expected_subscription_version=8,
    )


@pytest.mark.anyio
async def test_transactional_signal_port_returns_none_for_unknown_item() -> None:
    class Repository:
        def __init__(self, _session):
            pass

        async def get_signal_item(self, **_keys):
            return None

    result = await TransactionalQuoteSignalPort(
        object(), repository_factory=Repository
    ).get_quote_snapshot(item_id=uuid4(), cycle_id=uuid4())

    assert result is None
