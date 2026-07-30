from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.quotes.application import (
    QuoteApplication,
    TransactionalQuoteSignalPort,
)
from long_invest.modules.quotes.contracts import (
    QuoteItemStatus,
    RealtimeCheckMode,
    SignalQuoteSnapshot,
)


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

    def session(self):
        return Transaction()


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
async def test_manual_and_diagnostic_run_directly_without_jobs() -> None:
    class Runtime:
        def __init__(self):
            self.calls = []

        async def run(self, **values):
            self.calls.append(values)
            return SimpleNamespace(mode=values["mode"])

    class Calendar:
        async def is_trading_session(self, _at):
            return True

    runtime = Runtime()
    app = QuoteApplication(
        Database(), runtime_factory=lambda: runtime, calendar=Calendar()
    )
    common = {
        "symbols": ("600000.SH",),
        "idempotency_key": "same",
        "request_id": "req-1",
        "created_by_user_id": "user-1",
        "reason": "人工核对行情",
    }
    manual = await app.submit_manual(timeout_seconds=30, **common)
    diagnostic = await app.submit_diagnostic(
        session_id="session-1",
        trusted_ip="127.0.0.1",
        **common,
    )
    assert manual.mode is RealtimeCheckMode.MANUAL
    assert diagnostic.mode is RealtimeCheckMode.DIAGNOSTIC
    assert runtime.calls[0]["evaluate_signals"] is True
    assert runtime.calls[0]["timeout_seconds"] == 30
    assert runtime.calls[1]["evaluate_signals"] is False


@pytest.mark.anyio
async def test_allowed_actions_isolate_manual_and_diagnostic_active_jobs() -> None:
    database = Database()
    available = QuoteApplication(database)

    assert [item.value for item in await available.allowed_actions()] == [
        "MANUAL_COLLECT",
        "DIAGNOSE",
    ]
    assert database.transactions == []


@pytest.mark.anyio
async def test_manual_outside_session_never_evaluates_signals() -> None:
    class Runtime:
        async def run(self, **values):
            return SimpleNamespace(**values)

    class Calendar:
        async def is_trading_session(self, _at):
            return False

    app = QuoteApplication(
        Database(), runtime_factory=Runtime, calendar=Calendar()
    )
    values = {
        "symbols": ("600000.SH",),
        "timeout_seconds": 60,
        "idempotency_key": "same",
        "request_id": "request-1",
        "created_by_user_id": "user-1",
        "reason": "补采行情",
    }
    result = await app.submit_manual(**values)
    assert result.mode is RealtimeCheckMode.DIAGNOSTIC
    assert result.evaluate_signals is False


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
