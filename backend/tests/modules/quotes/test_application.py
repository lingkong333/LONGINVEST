from types import SimpleNamespace

import pytest

from long_invest.modules.quotes.application import QuoteApplication


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

    def __init__(self, session):
        self.session = session
        self.sessions.append(session)

    async def submit(self, command):
        self.commands.append(command)
        return SimpleNamespace(id="job-1", status="PENDING_DISPATCH")


@pytest.mark.anyio
async def test_manual_and_diagnostic_use_distinct_idempotent_job_types() -> None:
    Jobs.commands = []
    Jobs.sessions = []
    database = Database()
    app = QuoteApplication(database, job_service_factory=Jobs)
    common = {
        "symbols": ("600000.SH",),
        "idempotency_key": "same",
        "request_id": "req-1",
        "created_by_user_id": "user-1",
    }
    await app.submit_manual(timeout_seconds=30, **common)
    await app.submit_diagnostic(**common)
    assert [command.job_type for command in Jobs.commands] == [
        "REALTIME_QUOTE_CYCLE",
        "QUOTE_DIAGNOSTIC",
    ]
    assert Jobs.commands[0].config_snapshot["symbols"] == ["600000.SH"]
    assert "universe_snapshot_id" not in Jobs.commands[0].config_snapshot
    assert Jobs.commands[0].idempotency_scope != Jobs.commands[1].idempotency_scope
    assert Jobs.sessions == [
        transaction.session for transaction in database.transactions
    ]
