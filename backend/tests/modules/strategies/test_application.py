import asyncio
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

import long_invest.modules.strategies.application as strategy_application_module
from long_invest.modules.strategies.application import (
    StrategyApplication,
    UnconfiguredValidationEvidenceVerifier,
    get_configured_validation_evidence_verifier,
)
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


class Verifier:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def verify(self, claim):
        self.calls.append(claim)
        return self.result


class ReferenceVerifier:
    def __init__(self, persisted):
        self.persisted = persisted

    async def verify(self, claim):
        for name, check in claim.checks.items():
            key = (
                name,
                check["run_id"],
                check["task_id"],
                check["snapshot_id"],
            )
            fact = self.persisted.get(key)
            if fact is None or fact != {
                "status": "SUCCEEDED",
                "strategy_id": str(claim.strategy_id),
                "draft_version": claim.draft_version,
                "source_code_hash": claim.source_code_hash,
                "training_data_hash": check.get("training_data_hash"),
                "test_data_hash": check.get("test_data_hash"),
            }:
                return False
        return True


class Service:
    def __init__(self, version, run, evidence):
        self.version = version
        self.run = run
        self.evidence = evidence
        self.failed = []
        self.completed = []
        self.fail_complete_once = False

    async def get_validation_evidence(self, _validation_id):
        return SimpleNamespace(
            id=self.version.validation_run_id,
            strategy_id=self.version.strategy_id,
            draft_version=1,
            source_code_hash=self.version.source_code_hash,
            evidence_snapshot=self.evidence,
        )

    async def begin_publish(self, *_args, **_kwargs):
        return FrozenPublication(self.version, self.run)

    async def claim_publish_run(self, *_args, **_kwargs):
        self.run.status = "RUNNING"
        return FrozenPublication(self.version, self.run)

    async def complete_publish_run(self, run_id, **kwargs):
        if self.fail_complete_once:
            self.fail_complete_once = False
            raise OSError("crash after Git commit")
        self.completed.append((run_id, kwargs["git_commit"]))
        self.version.status = "PUBLISHED"
        self.run.status = "SUCCEEDED"
        return self.version

    async def fail_publish_run(self, run_id, error_code, **_kwargs):
        self.failed.append((run_id, error_code))
        if self.version.status == "PUBLISHED":
            self.run.status = "SUCCEEDED"
            return self.version
        self.version.status = "PUBLISH_FAILED"
        self.run.status = "FAILED"
        return self.version

    async def list_recoverable_publish_runs(self):
        return [self.run]


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

    def verify_source(self, **_kwargs):
        return True


def release_and_run():
    validation_id = uuid4()
    version = SimpleNamespace(
        id=uuid4(),
        strategy_id=uuid4(),
        version_no=1,
        source_code="source",
        source_code_hash=hashlib.sha256(b"source").hexdigest(),
        strategy_metadata={"name": "策略"},
        parameter_schema={"type": "object"},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "2" * 64,
        validation_run_id=validation_id,
        status="PUBLISHING",
    )
    return version, SimpleNamespace(id=uuid4(), status="PENDING")


def evidence(version):
    metadata_hash = _hash({"name": "策略"})
    schema_hash = _hash({"type": "object"})
    parameter_hash = _hash({})
    environment_hash = hashlib.sha256(b"python-3.12").hexdigest()
    facts = {
        "source_code_hash": version.source_code_hash,
        "metadata_hash": metadata_hash,
        "parameter_schema_hash": schema_hash,
        "parameter_hash": parameter_hash,
        "environment_hash": environment_hash,
        "runner_image_digest": version.runner_image_digest,
    }
    common = {
        "run_id": str(uuid4()),
        "task_id": str(uuid4()),
        "snapshot_id": str(uuid4()),
        "status": "SUCCEEDED",
        **facts,
    }
    training = {
        "training_start": "2010-01-01",
        "training_end": "2020-12-31",
        "training_data_hash": "b" * 64,
    }
    return {
        "schema_version": 1,
        "source_code_hash": version.source_code_hash,
        "metadata": {"name": "策略"},
        "metadata_hash": metadata_hash,
        "parameter_schema": {"type": "object"},
        "parameter_schema_hash": schema_hash,
        "params": {},
        "parameter_hash": parameter_hash,
        "environment_version": "python-3.12",
        "environment_hash": environment_hash,
        "runner_image_digest": version.runner_image_digest,
        "checks": {
            "static_analysis": dict(common),
            "fixed_sample": {**common, **training},
            "specified_stock": {
                **common,
                **training,
                "security_id": str(uuid4()),
            },
            "holdout_backtest": {
                **common,
                **training,
                "security_id": str(uuid4()),
                "test_start": "2021-01-01",
                "test_end": "2022-12-31",
                "test_data_hash": "c" * 64,
            },
        },
    }


def _hash(value):
    import json

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def application(database, service, git_store, verifier):
    return StrategyApplication(
        database,
        git_store=git_store,
        evidence_verifier=verifier,
        repository_factory=lambda _session: SimpleNamespace(),
        audit_factory=lambda _session: SimpleNamespace(),
        event_factory=lambda _session: SimpleNamespace(),
        service_factory=lambda *_args, **_kwargs: service,
    )


def publish_kwargs(version):
    return {
        "strategy_id": version.strategy_id,
        "validation_run_id": version.validation_run_id,
        "expected_draft_version": 1,
        "reason": "确认发布",
        "idempotency_key": "publish-1",
        "request_id": "req-1",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
    }


def persisted_references(version, snapshot):
    return {
        (
            name,
            check["run_id"],
            check["task_id"],
            check["snapshot_id"],
        ): {
            "status": "SUCCEEDED",
            "strategy_id": str(version.strategy_id),
            "draft_version": 1,
            "source_code_hash": version.source_code_hash,
            "training_data_hash": check.get("training_data_hash"),
            "test_data_hash": check.get("test_data_hash"),
        }
        for name, check in snapshot["checks"].items()
    }


def test_publish_only_freezes_and_returns_persistent_run():
    database = Database()
    version, run = release_and_run()
    snapshot = evidence(version)
    service = Service(version, run, snapshot)
    git_store = GitStore(database)
    verifier = ReferenceVerifier(persisted_references(version, snapshot))
    subject = application(database, service, git_store, verifier)

    result = asyncio.run(subject.publish(**publish_kwargs(version)))

    assert result.run.id == run.id
    assert git_store.calls == []
    assert database.transactions == 1


def test_unconfigured_production_verifier_fails_closed(monkeypatch):
    monkeypatch.setattr(
        strategy_application_module,
        "_validation_evidence_verifier_factory",
        None,
    )
    verifier = get_configured_validation_evidence_verifier()
    assert isinstance(verifier, UnconfiguredValidationEvidenceVerifier)
    database = Database()
    version, run = release_and_run()
    service = Service(version, run, evidence(version))
    subject = application(
        database,
        service,
        GitStore(database),
        verifier,
    )

    with pytest.raises(AppError) as raised:
        asyncio.run(subject.publish(**publish_kwargs(version)))

    assert raised.value.code == "STRATEGY_VALIDATION_STALE"
    assert database.transactions == 0


def test_cross_module_verifier_rejects_fabricated_references():
    database = Database()
    version, run = release_and_run()
    service = Service(version, run, evidence(version))
    subject = application(
        database,
        service,
        GitStore(database),
        ReferenceVerifier({}),
    )

    with pytest.raises(AppError) as raised:
        asyncio.run(subject.publish(**publish_kwargs(version)))

    assert raised.value.code == "STRATEGY_VALIDATION_STALE"


def test_worker_executes_git_outside_transaction_then_completes():
    database = Database()
    version, run = release_and_run()
    service = Service(version, run, evidence(version))
    subject = application(database, service, GitStore(database), Verifier())

    result = asyncio.run(subject.execute_publish(run.id))

    assert result.status == "PUBLISHED"
    assert service.completed == [(run.id, "a" * 40)]
    assert database.transactions == 2


def test_publish_fails_closed_when_evidence_cannot_be_reverified():
    database = Database()
    version, run = release_and_run()
    service = Service(version, run, evidence(version))
    subject = application(database, service, GitStore(database), Verifier(False))

    with pytest.raises(AppError) as raised:
        asyncio.run(subject.publish(**publish_kwargs(version)))

    assert raised.value.code == "STRATEGY_VALIDATION_STALE"
    assert database.transactions == 0


def test_git_failure_is_persisted_on_run_and_safe_to_recover():
    database = Database()
    version, run = release_and_run()
    service = Service(version, run, evidence(version))
    subject = application(
        database,
        service,
        GitStore(database, failure=OSError("disk")),
        Verifier(),
    )

    with pytest.raises(AppError) as raised:
        asyncio.run(subject.execute_publish(run.id))

    assert raised.value.code == "STRATEGY_PUBLISH_FAILED"
    assert service.failed == [(run.id, "STRATEGY_GIT_FAILED")]


def test_recovery_replays_git_after_crash_before_database_completion():
    database = Database()
    version, run = release_and_run()
    service = Service(version, run, evidence(version))
    service.fail_complete_once = True
    git_store = GitStore(database)
    subject = application(database, service, git_store, Verifier())

    with pytest.raises(AppError):
        asyncio.run(subject.execute_publish(run.id))
    result = asyncio.run(subject.execute_publish(run.id))

    assert result.status == "PUBLISHED"
    assert len(git_store.calls) == 2


def test_commit_confirmation_error_does_not_downgrade_published_run():
    database = Database()
    version, run = release_and_run()
    service = Service(version, run, evidence(version))
    original_complete = service.complete_publish_run

    async def commit_then_raise(*args, **kwargs):
        result = await original_complete(*args, **kwargs)
        assert result.status == "PUBLISHED"
        raise OSError("connection lost after commit")

    service.complete_publish_run = commit_then_raise
    subject = application(database, service, GitStore(database), Verifier())

    with pytest.raises(AppError) as raised:
        asyncio.run(subject.execute_publish(run.id))

    assert raised.value.code == "STRATEGY_PUBLISH_FAILED"
    assert version.status == "PUBLISHED"
    assert run.status == "SUCCEEDED"
