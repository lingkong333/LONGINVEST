from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.securities.application import SecurityApplication
from long_invest.modules.securities.contracts import SecurityAuditContext
from long_invest.platform.errors import AppError


class FakeDatabase:
    def __init__(self) -> None:
        self.session = Mock()

    @asynccontextmanager
    async def transaction(self):
        yield self.session


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
