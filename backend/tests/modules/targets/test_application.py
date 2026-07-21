from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.targets.application import (
    TargetApplication,
    transactional_target_snapshot_port,
)
from long_invest.modules.targets.strategy_service import CalculationResult
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


@pytest.mark.anyio
async def test_strategy_snapshot_failure_is_persisted_as_failed_run() -> None:
    application = object.__new__(TargetApplication)
    application._strategy_application = SimpleNamespace(
        get_execution_snapshot=AsyncMock(side_effect=RuntimeError("snapshot down"))
    )
    application._training_data = object()
    application._forecast = object()
    failed = CalculationResult("TARGET_CALCULATION_FAILED", uuid4())
    application._strategy_write = AsyncMock(return_value=failed)
    reservation = SimpleNamespace(
        replayed=False,
        status="PENDING",
        run_id=failed.run_id,
        strategy_version_id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        parameter_snapshot={},
    )
    command = SimpleNamespace(
        target_date=date(2026, 7, 21),
        training_start_date=date(2020, 1, 1),
        training_end_date=date(2025, 12, 31),
    )

    result = await application._execute_reservation(command, reservation)

    assert result is failed
    application._strategy_write.assert_awaited_once()
    assert application._strategy_write.await_args.args[:2] == (
        "fail",
        failed.run_id,
    )


@pytest.mark.anyio
async def test_strategy_batch_isolates_one_subscription_failure() -> None:
    application = object.__new__(TargetApplication)
    succeeded = CalculationResult("TARGET_CALCULATION_SUCCEEDED", uuid4())
    application.apply_strategy = AsyncMock(
        side_effect=[
            AppError(code="ONE_FAILED", message="failed", status_code=409),
            succeeded,
        ]
    )
    first_id, second_id = uuid4(), uuid4()
    commands = (
        SimpleNamespace(calculation=SimpleNamespace(subscription_id=first_id)),
        SimpleNamespace(calculation=SimpleNamespace(subscription_id=second_id)),
    )

    results = await application.apply_strategy_batch(commands)

    assert results[0] == (first_id, "ONE_FAILED", None)
    assert results[1] == (second_id, succeeded.code, succeeded)
