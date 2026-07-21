from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.targets.application import (
    TargetApplication,
    transactional_target_snapshot_port,
)
from long_invest.platform.errors import AppError


class Database:
    def __init__(self, *, fail=False):
        self.session_object = object()
        self.fail = fail

    @asynccontextmanager
    async def transaction(self):
        if self.fail:
            raise SQLAlchemyError("down")
        yield self.session_object

    @asynccontextmanager
    async def session(self):
        if self.fail:
            raise TimeoutError
        yield self.session_object


@pytest.mark.anyio
async def test_write_binds_every_port_to_same_transaction() -> None:
    database = Database()
    seen = []

    def factory(name, value):
        def build(session):
            assert session is database.session_object
            seen.append(name)
            return value

        return build

    expected = object()

    async def set_manual(_command):
        return expected

    service = SimpleNamespace(set_manual=set_manual)
    application = TargetApplication(
        database,
        subscription_factory=factory("subscription", object()),
        repository_factory=factory("repository", object()),
        audit_factory=factory("audit", object()),
        event_factory=factory("event", object()),
        service_factory=lambda repo, **ports: seen.append("service") or service,
    )

    result = await application.set_manual(object())

    assert result is expected
    assert seen == ["repository", "subscription", "audit", "event", "service"]


@pytest.mark.anyio
async def test_database_failure_maps_to_stable_error() -> None:
    application = TargetApplication(
        Database(fail=True), subscription_factory=lambda session: session
    )

    with pytest.raises(AppError) as caught:
        await application.set_manual(object())

    assert caught.value.code == "TARGET_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["list", "get", "history"])
async def test_read_uses_session_and_maps_timeout(method) -> None:
    application = TargetApplication(
        Database(fail=True), subscription_factory=lambda session: session
    )

    with pytest.raises(AppError) as caught:
        if method == "list":
            await application.list(page=1, page_size=50)
        elif method == "history":
            await application.history("subscription-id", page=1, page_size=50)
        else:
            await getattr(application, method)("subscription-id")

    assert caught.value.code == "TARGET_BACKEND_UNAVAILABLE"


@pytest.mark.anyio
async def test_transactional_snapshot_port_uses_caller_session() -> None:
    session = object()

    class Repository:
        def __init__(self, received_session):
            assert received_session is session

        async def get_binding(self, subscription_id):
            return None

    port = transactional_target_snapshot_port(session, repository_factory=Repository)
    assert await port.get_target_snapshot("subscription-id") is None
