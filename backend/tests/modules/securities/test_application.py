from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

import long_invest.modules.securities.application as application_module
from long_invest.modules.securities.application import SecurityApplication
from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityAuditContext,
    SecurityIdentity,
    SecurityType,
)
from long_invest.platform.errors import AppError


class FakeDatabase:
    def __init__(self) -> None:
        self.session = Mock()

    @asynccontextmanager
    async def transaction(self):
        yield self.session


class IdentityDatabase:
    def __init__(self) -> None:
        self.db_session = Mock()

    @asynccontextmanager
    async def session(self):
        yield self.db_session


@pytest.mark.anyio
async def test_resolve_identity_returns_a_public_snapshot(monkeypatch) -> None:
    database = IdentityDatabase()
    security_id = uuid4()
    security = SimpleNamespace(
        id=security_id,
        symbol="600000.SH",
        market="SH",
        security_type="A_SHARE",
        listing_status="LISTED",
        is_suspended=False,
        is_st=True,
        listed_on=date(1999, 11, 10),
        delisted_on=None,
        master_version=7,
        _sa_instance_state=object(),
    )
    captured = {}

    class Repository:
        def __init__(self, session) -> None:
            captured["session"] = session

        async def get_by_symbol(self, symbol: str):
            captured["symbol"] = symbol
            return security

    monkeypatch.setattr(application_module, "SecurityRepository", Repository)

    identity = await SecurityApplication(database).resolve_identity("600000.SH")

    assert identity == SecurityIdentity(
        security_id=security_id,
        symbol="600000.SH",
        market=Market.SH,
        security_type=SecurityType.A_SHARE,
        listing_status=ListingStatus.LISTED,
        is_suspended=False,
        is_st=True,
        listed_on=date(1999, 11, 10),
        delisted_on=None,
        master_version=7,
    )
    assert captured == {
        "session": database.db_session,
        "symbol": "600000.SH",
    }
    assert not hasattr(identity, "_sa_instance_state")
    assert not hasattr(identity, "__dict__")


@pytest.mark.anyio
async def test_resolve_identity_rejects_invalid_symbol_without_database() -> None:
    class DatabaseThatMustNotBeUsed:
        def session(self):
            raise AssertionError("database must not be accessed")

    with pytest.raises(AppError) as captured:
        await SecurityApplication(DatabaseThatMustNotBeUsed()).resolve_identity(
            "600000"
        )

    assert captured.value.code == "SECURITY_SYMBOL_INVALID"
    assert captured.value.status_code == 422


@pytest.mark.anyio
async def test_freeze_symbols_can_join_a_caller_owned_transaction(monkeypatch) -> None:
    session = object()
    snapshot_id = uuid4()
    calls = {}

    class Repository:
        def __init__(self, value) -> None:
            calls["session"] = value

        async def get_universe_snapshot(self, value):
            calls["snapshot_id"] = value
            return SimpleNamespace(id=value, master_version=4, items=())

    class Service:
        def __init__(self, repository) -> None:
            calls["repository"] = repository

        async def freeze_symbols(self, query):
            calls["symbols"] = query.symbols
            return SimpleNamespace(id=snapshot_id)

    monkeypatch.setattr(application_module, "SecurityRepository", Repository)
    monkeypatch.setattr(application_module, "SecurityMasterService", Service)
    application = SecurityApplication(FakeDatabase())

    frozen = await application.freeze_symbols_in_transaction(
        session, ("600000.SH",)
    )

    assert frozen.id == snapshot_id
    assert frozen.master_version == 4
    assert calls["session"] is session
    assert calls["symbols"] == ("600000.SH",)


@pytest.mark.anyio
async def test_resolve_identity_reports_a_missing_security(monkeypatch) -> None:
    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def get_by_symbol(self, _symbol: str):
            return None

    monkeypatch.setattr(application_module, "SecurityRepository", Repository)

    with pytest.raises(AppError) as captured:
        await SecurityApplication(IdentityDatabase()).resolve_identity("600000.SH")

    assert captured.value.code == "SECURITY_NOT_FOUND"
    assert captured.value.status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [SQLAlchemyError("database down"), TimeoutError()])
async def test_resolve_identity_maps_database_failures_to_stable_503(
    monkeypatch, failure
) -> None:
    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def get_by_symbol(self, _symbol: str):
            raise failure

    monkeypatch.setattr(application_module, "SecurityRepository", Repository)

    with pytest.raises(AppError) as captured:
        await SecurityApplication(IdentityDatabase()).resolve_identity("600000.SH")

    assert captured.value.code == "SECURITY_BACKEND_UNAVAILABLE"
    assert captured.value.status_code == 503


@pytest.mark.anyio
async def test_refresh_submits_public_job_in_database_transaction() -> None:
    database = FakeDatabase()
    captured = {}
    job = Mock(
        id=uuid4(),
        status="PENDING_DISPATCH",
        job_type="SECURITY_MASTER_REFRESH",
    )

    class FakeJobService:
        def __init__(self, session) -> None:
            captured["session"] = session

        async def submit(self, command):
            captured["command"] = command
            return job

    application = SecurityApplication(
        database,
        job_service_factory=FakeJobService,
    )

    submitted = await application.refresh(
        idempotency_key="refresh-key",
        request_id="request-1",
        created_by_user_id="user-1",
    )

    assert submitted is job
    assert captured["session"] is database.session
    command = captured["command"]
    assert command.job_type == "SECURITY_MASTER_REFRESH"
    assert command.queue == "maintenance"
    assert command.idempotency_scope == "securities:refresh"
    assert command.idempotency_key == "refresh-key"
    assert command.created_by_user_id == "user-1"
    assert command.config_snapshot == {
        "source": "eastmoney",
        "idempotency_key": "refresh-key",
        "request_id": "request-1",
        "created_by_user_id": "user-1",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [SQLAlchemyError("database down"), TimeoutError()])
async def test_database_and_timeout_failures_are_stable_503(failure) -> None:
    class FailingDatabase:
        @asynccontextmanager
        async def session(self):
            raise failure
            yield  # pragma: no cover

    application = SecurityApplication(FailingDatabase())

    with pytest.raises(AppError) as captured:
        await application.list(page=1, page_size=20)

    assert captured.value.code == "SECURITY_BACKEND_UNAVAILABLE"
    assert captured.value.status_code == 503


@pytest.mark.anyio
async def test_business_idempotency_conflict_is_not_rewritten_as_503() -> None:
    database = FakeDatabase()

    class ConflictingJobService:
        def __init__(self, _session) -> None:
            pass

        async def submit(self, _command):
            raise AppError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="幂等键冲突",
                status_code=409,
            )

    application = SecurityApplication(
        database,
        job_service_factory=ConflictingJobService,
    )

    with pytest.raises(AppError) as captured:
        await application.refresh(
            idempotency_key="same-key",
            request_id="request-1",
            created_by_user_id="user-1",
        )

    assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert captured.value.status_code == 409


@pytest.mark.anyio
async def test_event_failure_rolls_back_business_revision_version_and_audit() -> None:
    class AtomicDatabase:
        def __init__(self) -> None:
            self.db_session = Mock()
            self.rolled_back = False
            self.state = {
                "business": [],
                "revisions": [],
                "versions": [],
                "audits": [],
                "outbox": [],
            }

        @asynccontextmanager
        async def transaction(self):
            before = deepcopy(self.state)
            try:
                yield self.db_session
            except Exception:
                self.state = before
                self.rolled_back = True
                raise

    database = AtomicDatabase()
    captured = {}

    class Audit:
        def __init__(self, session) -> None:
            self.session = session

        async def record(self, _event) -> None:
            database.state["audits"].append("audit")

    class Events:
        def __init__(self, session, _writer) -> None:
            self.session = session

        async def publish(self, _event) -> None:
            database.state["outbox"].append("event")
            raise RuntimeError("forced outbox failure")

    class MasterService:
        def __init__(self, session, **kwargs) -> None:
            captured["service_session"] = session
            captured["audit"] = kwargs["audit"]
            captured["events"] = kwargs["events"]

        async def apply_snapshot(self, _snapshot):
            database.state["business"].append("security")
            database.state["revisions"].append("revision")
            database.state["versions"].append("version")
            await captured["audit"].record(Mock())
            await captured["events"].publish(Mock())

    application = SecurityApplication(
        database,
        outbox_writer=Mock(),
        audit_factory=Audit,
        event_factory=Events,
        master_service_factory=MasterService,
    )
    context = SecurityAuditContext(
        request_id="req_12345678",
        idempotency_key="refresh-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        reason="manual refresh",
    )

    with pytest.raises(RuntimeError, match="forced outbox failure"):
        await application.apply_snapshot(Mock(), audit_context=context)

    assert database.rolled_back is True
    assert database.state == {
        "business": [],
        "revisions": [],
        "versions": [],
        "audits": [],
        "outbox": [],
    }
    assert captured["service_session"] is database.db_session
    assert captured["audit"].session is database.db_session
    assert captured["events"].session is database.db_session
