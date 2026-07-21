import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.strategies.application import StrategyApplication
from long_invest.modules.strategies.service import FrozenPublication
from long_invest.platform.errors import AppError


class Database:
    def __init__(self):
        self.in_transaction = False
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        self.in_transaction = True
        try:
            yield SimpleNamespace()
        finally:
            self.in_transaction = False

    @asynccontextmanager
    async def session(self):
        yield SimpleNamespace()


class Service:
    def __init__(self, version):
        self.version = version
        self.failed = []
        self.completed = []

    async def begin_publish(self, *_args, **_kwargs):
        return FrozenPublication(self.version)

    async def complete_publish(self, strategy_id, version_id, **kwargs):
        self.completed.append((strategy_id, version_id, kwargs["git_commit"]))
        self.version.status = "PUBLISHED"
        return self.version

    async def fail_publish(self, strategy_id, version_id, error_code, **kwargs):
        self.failed.append((strategy_id, version_id, error_code))
        self.version.status = "PUBLISH_FAILED"
        return self.version


class GitStore:
    def __init__(self, database, *, failure=None):
        self.database = database
        self.failure = failure
        self.calls = []

    def publish(self, **kwargs):
        assert not self.database.in_transaction
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return "a" * 40


def version():
    return SimpleNamespace(
        id=uuid4(),
        strategy_id=uuid4(),
        version_no=1,
        source_code="source",
        source_code_hash="1" * 64,
        strategy_metadata={"name": "策略"},
        parameter_schema={"type": "object"},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "2" * 64,
        validation_run_id=uuid4(),
        status="PUBLISHING",
    )


def application(database, service, git_store):
    return StrategyApplication(
        database,
        git_store=git_store,
        repository_factory=lambda _session: SimpleNamespace(),
        audit_factory=lambda _session: SimpleNamespace(),
        event_factory=lambda _session: SimpleNamespace(),
        service_factory=lambda *_args, **_kwargs: service,
    )


def publish_kwargs(strategy_id, validation_id):
    return {
        "strategy_id": strategy_id,
        "validation_run_id": validation_id,
        "expected_draft_version": 1,
        "reason": "确认发布",
        "idempotency_key": "publish-1",
        "request_id": "req-1",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
    }


def test_publish_writes_git_outside_database_transaction_then_completes():
    database = Database()
    release = version()
    service = Service(release)
    git_store = GitStore(database)
    subject = application(database, service, git_store)

    result = asyncio.run(
        subject.publish(
            **publish_kwargs(release.strategy_id, release.validation_run_id)
        )
    )

    assert result.status == "PUBLISHED"
    assert database.transactions == 2
    assert service.completed == [(release.strategy_id, release.id, "a" * 40)]


def test_git_failure_is_persisted_as_publish_failed_and_safe_to_retry():
    database = Database()
    release = version()
    service = Service(release)
    subject = application(
        database, service, GitStore(database, failure=OSError("disk"))
    )

    with pytest.raises(AppError) as raised:
        asyncio.run(
            subject.publish(
                **publish_kwargs(release.strategy_id, release.validation_run_id)
            )
        )

    assert raised.value.code == "STRATEGY_PUBLISH_FAILED"
    assert service.failed == [(release.strategy_id, release.id, "STRATEGY_GIT_FAILED")]
    assert database.transactions == 2
