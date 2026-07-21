from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.signals.application import SignalApplication
from long_invest.platform.errors import AppError


class Database:
    def __init__(self):
        self.session_value = object()
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield self.session_value

    @asynccontextmanager
    async def session(self):
        yield self.session_value


@pytest.mark.anyio
async def test_evaluate_binds_all_ports_to_one_transaction():
    database = Database()
    seen = []

    def factory(name):
        return lambda session: seen.append((name, session)) or object()

    class Service:
        def __init__(self, repository, **ports):
            assert repository is not None
            assert set(ports) == {
                "subscriptions",
                "targets",
                "quotes",
                "positions",
                "notifications",
                "events",
            }

        async def evaluate(self, command):
            return command

    app = SignalApplication(
        database,
        repository_factory=factory("repository"),
        subscription_factory=factory("subscription"),
        target_factory=factory("target"),
        quote_factory=factory("quote"),
        position_factory=factory("position"),
        notification_factory=factory("notification"),
        service_factory=Service,
    )
    assert await app.evaluate("command") == "command"
    assert database.transactions == 1
    assert {session for _, session in seen} == {database.session_value}


@pytest.mark.anyio
async def test_backend_errors_are_stable():
    database = Database()

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate(self, command):
            raise SQLAlchemyError("down")

    app = SignalApplication(
        database,
        repository_factory=lambda s: object(),
        subscription_factory=lambda s: object(),
        target_factory=lambda s: object(),
        quote_factory=lambda s: object(),
        position_factory=lambda s: object(),
        notification_factory=lambda s: object(),
        service_factory=Service,
    )
    with pytest.raises(AppError) as exc:
        await app.evaluate(object())
    assert exc.value.code == "SIGNAL_BACKEND_UNAVAILABLE"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("list_states", (), {"page": 1, "page_size": 20}),
        ("list_evaluations", (), {"page": 1, "page_size": 20}),
        ("list_events", (), {"page": 1, "page_size": 20}),
        ("get_state", ("subscription-id",), {}),
        ("get_evaluation", ("evaluation-id",), {}),
        ("get_event", ("event-id",), {}),
    ],
)
async def test_reads_use_read_only_session(method, args, kwargs):
    database = Database()
    expected = object()

    class Service:
        def __init__(self, *args, **ports):
            pass

        def __getattr__(self, name):
            async def call(*received_args, **received_kwargs):
                assert name == method
                assert received_args == args
                assert received_kwargs == kwargs
                return expected

            return call

    app = SignalApplication(
        database,
        repository_factory=lambda s: object(),
        subscription_factory=lambda s: object(),
        target_factory=lambda s: object(),
        quote_factory=lambda s: object(),
        position_factory=lambda s: object(),
        notification_factory=lambda s: object(),
        service_factory=Service,
    )

    assert await getattr(app, method)(*args, **kwargs) is expected
    assert database.transactions == 0


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["reset", "reevaluate"])
async def test_signal_writes_bind_audit_event_and_job_to_one_transaction(method):
    database = Database()
    seen = []

    def factory(name):
        return lambda session: seen.append((name, session)) or object()

    class Service:
        def __init__(self, repository, **ports):
            assert repository is not None
            assert set(ports) == {
                "subscriptions",
                "targets",
                "quotes",
                "positions",
                "notifications",
                "audit",
                "events",
                "jobs",
            }

        def __getattr__(self, name):
            async def call(command):
                assert name == method
                return command

            return call

    app = SignalApplication(
        database,
        repository_factory=factory("repository"),
        subscription_factory=factory("subscription"),
        target_factory=factory("target"),
        quote_factory=factory("quote"),
        position_factory=factory("position"),
        notification_factory=factory("notification"),
        audit_factory=factory("audit"),
        event_factory=factory("event"),
        job_factory=factory("job"),
        service_factory=Service,
    )

    assert await getattr(app, method)("command") == "command"
    assert database.transactions == 1
    assert {session for _, session in seen} == {database.session_value}
