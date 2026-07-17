from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import time
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.monitor_schedules.application import (
    MonitorScheduleApplication,
    ScheduleAuditAdapter,
    _audit_key,
)
from long_invest.platform.errors import AppError


class Database:
    def __init__(self, error=None):
        self.error = error

    @asynccontextmanager
    async def session(self):
        if self.error:
            raise self.error
        yield object()

    @asynccontextmanager
    async def transaction(self):
        if self.error:
            raise self.error
        yield object()


class Service:
    current = None

    def __init__(self, repository, **kwargs):
        pass

    async def current_revision(self, schedule_id):
        return self.current


@pytest.mark.anyio
async def test_current_revision_returns_public_frozen_contract() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import uuid4

    revision = SimpleNamespace(
        id=uuid4(),
        schedule_id=uuid4(),
        revision_no=2,
        times=["09:45", "14:30"],
        timezone="Asia/Shanghai",
        reason="调整",
        created_at=datetime.now(UTC),
    )
    Service.current = revision
    app = MonitorScheduleApplication(
        Database(), repository_factory=lambda session: object(), service_factory=Service
    )
    result = await app.current_revision(revision.schedule_id)
    assert result.model_config["frozen"] is True
    assert result.times == (time(9, 45), time(14, 30))


@pytest.mark.anyio
async def test_database_timeout_is_mapped_to_503() -> None:
    app = MonitorScheduleApplication(
        Database(SQLAlchemyError("down")),
        repository_factory=lambda session: object(),
        service_factory=Service,
    )
    with pytest.raises(AppError) as caught:
        await app.list()
    assert caught.value.code == "MONITOR_SCHEDULE_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503


@pytest.mark.anyio
async def test_transaction_rolls_back_when_outbox_fails() -> None:
    class TransactionDatabase(Database):
        def __init__(self):
            super().__init__()
            self.state = []

        @asynccontextmanager
        async def transaction(self):
            before = deepcopy(self.state)
            try:
                yield self
            except Exception:
                self.state = before
                raise

    class Repository:
        def __init__(self, database):
            self.database = database

    class FailingOutboxService:
        def __init__(self, repository, **kwargs):
            self.database = repository.database

        async def create(self, definition, **context):
            self.database.state.append(definition.name)
            raise SQLAlchemyError("outbox unavailable")

    database = TransactionDatabase()
    app = MonitorScheduleApplication(
        database,
        repository_factory=Repository,
        service_factory=FailingOutboxService,
    )
    with pytest.raises(AppError) as caught:
        await app.create(
            SimpleNamespace(name="盘中检查"),
            request_id="req-1",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
        )
    assert caught.value.code == "MONITOR_SCHEDULE_BACKEND_UNAVAILABLE"
    assert database.state == []


def test_audit_idempotency_key_is_bounded_and_content_sensitive() -> None:
    from uuid import uuid4

    schedule_id = uuid4()
    first = _audit_key(schedule_id, "x" * 200, "updated")
    second = _audit_key(schedule_id, "y" * 200, "updated")
    assert len(first) <= 160
    assert first != second


@pytest.mark.anyio
async def test_audit_adapter_persists_digest_and_real_summaries(monkeypatch) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    import long_invest.modules.monitor_schedules.application as module

    class Audit:
        written = None

        def __init__(self, session):
            pass

        async def append(self, item):
            type(self).written = item

        async def find_by_idempotency(self, key):
            return SimpleNamespace(
                object_id=str(event.schedule_id),
                after_summary=type(self).written.after_summary,
            )

    monkeypatch.setattr(module, "AuditService", Audit)
    event = SimpleNamespace(
        schedule_id=uuid4(),
        revision_id=uuid4(),
        version=2,
        times=("14:30",),
        reason="调整",
        action="updated",
        request_id="req-1",
        idempotency_key="update-1",
        request_digest="a" * 64,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        before_summary={"name": "早盘", "version": 1},
        after_summary={
            "name": "午后",
            "version": 2,
            "revision_id": "placeholder",
            "times": ["14:30"],
            "archived": False,
        },
    )
    event.after_summary["revision_id"] = str(event.revision_id)
    adapter = ScheduleAuditAdapter(object())
    await adapter.record(event)
    assert Audit.written.before_summary == event.before_summary
    assert Audit.written.after_summary["_request_digest"] == "a" * 64
    replay = await adapter.find_replay(
        action="updated",
        schedule_id=event.schedule_id,
        idempotency_key="update-1",
    )
    assert replay.request_digest == "a" * 64
    assert replay.after_summary == event.after_summary
