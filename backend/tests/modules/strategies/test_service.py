from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.strategies.service import (
    PublishEvidence,
    StrategyCommandContext,
    StrategyService,
)
from long_invest.platform.errors import AppError


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class FakeRepository:
    def __init__(self) -> None:
        self.strategies = {}
        self.drafts = {}
        self.revisions = {}
        self.versions = {}
        self.validation_runs = {}
        self.runs = {}

    async def list_strategies(self, **_kwargs):
        rows = list(self.strategies.values())
        if not _kwargs["include_archived"]:
            rows = [row for row in rows if row.status != "ARCHIVED"]
        return rows, len(rows)

    async def get_strategy(self, strategy_id, *, for_update=False):
        return self.strategies.get(strategy_id)

    async def get_draft(self, strategy_id, *, for_update=False):
        return self.drafts.get(strategy_id)

    async def create_strategy(self, strategy, draft):
        self.strategies[strategy.id] = strategy
        self.drafts[strategy.id] = draft

    async def update_draft(self, strategy_id, *, source_code, expected_version):
        draft = self.drafts[strategy_id]
        if draft.draft_version != expected_version:
            return None
        draft.source_code = source_code
        draft.draft_version += 1
        return draft

    async def add_revision(self, revision):
        self.revisions[revision.id] = revision

    async def add_validation_run(self, validation):
        self.validation_runs[validation.id] = validation

    async def next_revision_no(self, draft_id):
        return 1 + max(
            (r.revision_no for r in self.revisions.values() if r.draft_id == draft_id),
            default=0,
        )

    async def get_revision(self, strategy_id, revision_id):
        revision = self.revisions.get(revision_id)
        draft = self.drafts.get(strategy_id)
        return (
            revision
            if revision and draft and revision.draft_id == draft.id
            else None
        )

    async def list_revisions(self, strategy_id, **_kwargs):
        draft = self.drafts[strategy_id]
        rows = [r for r in self.revisions.values() if r.draft_id == draft.id]
        return rows, len(rows)

    async def set_strategy_status(self, strategy_id, status):
        self.strategies[strategy_id].status = status

    async def set_strategy_name(self, strategy_id, name):
        self.strategies[strategy_id].name = name

    async def rename_strategy(self, strategy_id, *, name, expected_version):
        draft = self.drafts[strategy_id]
        if draft.draft_version != expected_version:
            return None
        draft.draft_version += 1
        self.strategies[strategy_id].name = name
        return draft

    async def get_validation_run(self, validation_run_id, *, for_update=False):
        return self.validation_runs.get(validation_run_id)

    async def bind_validation_run(
        self,
        validation_run_id,
        version_id,
        *,
        strategy_id,
        draft_version,
        source_code_hash,
    ):
        validation = self.validation_runs.get(validation_run_id)
        if (
            validation is None
            or validation.strategy_id != strategy_id
            or validation.draft_version != draft_version
            or validation.source_code_hash != source_code_hash
            or validation.status != "SUCCEEDED"
            or validation.strategy_version_id is not None
        ):
            return False
        validation.strategy_version_id = version_id
        return True

    async def complete_validation_run(
        self, validation_run_id, *, status, error_code, evidence_snapshot, completed_at
    ):
        validation = self.validation_runs[validation_run_id]
        validation.status = status
        validation.error_code = error_code
        validation.evidence_snapshot = evidence_snapshot
        validation.completed_at = completed_at
        return validation

    async def next_version_no(self, strategy_id):
        return 1 + max(
            (
                v.version_no
                for v in self.versions.values()
                if v.strategy_id == strategy_id
            ),
            default=0,
        )

    async def add_version(self, version):
        self.versions[version.id] = version

    async def add_strategy_run(self, run):
        self.runs[run.id] = run

    async def get_strategy_run(self, run_id, *, for_update=False):
        return self.runs.get(run_id)

    async def get_publish_run_for_version(self, version_id, *, for_update=False):
        return next(
            (r for r in self.runs.values() if r.strategy_version_id == version_id),
            None,
        )

    async def set_strategy_run_status(self, run_id, status):
        self.runs[run_id].status = status

    async def list_recoverable_publish_runs(self):
        return [
            r
            for r in self.runs.values()
            if r.status in {"PENDING", "RUNNING", "FAILED"}
        ]

    async def get_version(self, strategy_id, version_id, *, for_update=False):
        version = self.versions.get(version_id)
        return version if version and version.strategy_id == strategy_id else None

    async def get_version_by_id(self, version_id, *, for_update=False):
        return self.versions.get(version_id)

    async def latest_published_version(self, strategy_id):
        rows = [v for v in self.versions.values() if v.strategy_id == strategy_id]
        return max(rows, key=lambda row: row.version_no, default=None)

    async def latest_failed_version(self, strategy_id, source_code_hash):
        return next(
            (
                row
                for row in self.versions.values()
                if row.strategy_id == strategy_id
                and row.source_code_hash == source_code_hash
                and row.status == "PUBLISH_FAILED"
            ),
            None,
        )

    async def list_versions(self, strategy_id, **_kwargs):
        rows = [v for v in self.versions.values() if v.strategy_id == strategy_id]
        return rows, len(rows)


class Recorder:
    def __init__(self) -> None:
        self.items = []

    async def append(self, item):
        self.items.append(item)

    async def find_by_idempotency(self, key):
        return next((item for item in self.items if item.idempotency_key == key), None)

    async def emit(self, item):
        self.items.append(item)


def context(key="request-1"):
    return StrategyCommandContext(
        request_id="req-1",
        idempotency_key=key,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        reason="日常维护",
    )


def service(repository=None):
    repository = repository or FakeRepository()
    audit, events = Recorder(), Recorder()
    return (
        StrategyService(repository, audit=audit, events=events),
        repository,
        audit,
        events,
    )


def succeeded_validation(
    strategy_id,
    draft_version,
    source_code_hash,
    *,
    metadata=None,
    parameter_schema=None,
    params=None,
):
    validation_id = uuid4()
    metadata = metadata or {"name": "策略"}
    parameter_schema = parameter_schema or {"type": "object"}
    params = params or {}
    source_facts = {
        "source_code_hash": source_code_hash,
        "metadata_hash": json_hash(metadata),
        "parameter_schema_hash": json_hash(parameter_schema),
        "parameter_hash": json_hash(params),
        "environment_hash": hashlib.sha256(b"python-3.12").hexdigest(),
        "runner_image_digest": "sha256:" + "a" * 64,
    }
    snapshot = {
        "schema_version": 1,
        "source_code_hash": source_code_hash,
        "metadata": metadata,
        "metadata_hash": source_facts["metadata_hash"],
        "parameter_schema": parameter_schema,
        "parameter_schema_hash": source_facts["parameter_schema_hash"],
        "params": params,
        "parameter_hash": source_facts["parameter_hash"],
        "environment_version": "python-3.12",
        "environment_hash": source_facts["environment_hash"],
        "runner_image_digest": source_facts["runner_image_digest"],
        "checks": validation_checks(source_facts),
    }
    return validation_id, SimpleNamespace(
        id=validation_id,
        strategy_id=strategy_id,
        strategy_version_id=None,
        draft_version=draft_version,
        source_code_hash=source_code_hash,
        evidence_snapshot=snapshot,
        status="SUCCEEDED",
        error_code=None,
        completed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )


def validation_checks(source_facts):
    common = {
        "run_id": str(uuid4()),
        "task_id": str(uuid4()),
        "snapshot_id": str(uuid4()),
        "status": "SUCCEEDED",
        **source_facts,
    }
    training = {
        "training_start": "2010-01-01",
        "training_end": "2020-12-31",
        "training_data_hash": "b" * 64,
    }
    return {
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
    }


def checks_for_run(run):
    snapshot = run.evidence_snapshot
    facts = {
        key: snapshot[key]
        for key in (
            "source_code_hash",
            "metadata_hash",
            "parameter_schema_hash",
            "parameter_hash",
            "environment_hash",
            "runner_image_digest",
        )
    }
    return validation_checks(facts)


def publish_evidence(validation, expected_draft_version):
    return PublishEvidence(
        validation_run_id=validation.id,
        expected_draft_version=expected_draft_version,
        evidence_hash=json_hash(validation.evidence_snapshot),
    )


def json_hash(value):
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode()).hexdigest()


@async_test
async def test_create_initializes_empty_server_draft_and_records_event():
    subject, repository, audit, events = service()

    result = await subject.create("均线策略", context())

    assert result.draft.source_code == ""
    assert result.draft.draft_version == 1
    assert repository.strategies[result.strategy.id].status == "DRAFT"
    assert audit.items[-1].action_code == "strategy.created"
    assert events.items[-1].topic == "strategy.created"


@async_test
async def test_repeated_create_replays_and_changed_payload_conflicts():
    subject, repository, *_ = service()

    first = await subject.create("均线策略", context("same-key"))
    replay = await subject.create("均线策略", context("same-key"))

    assert replay.strategy.id == first.strategy.id
    assert len(repository.strategies) == 1
    with pytest.raises(AppError) as raised:
        await subject.create("另一个策略", context("same-key"))
    assert raised.value.code == "STRATEGY_IDEMPOTENCY_CONFLICT"


@async_test
async def test_autosave_updates_draft_without_creating_revision():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())

    result = await subject.save_draft(
        created.strategy.id,
        source_code="def calculate_targets(): pass",
        expected_version=1,
        create_revision=False,
        context=context("save-1"),
    )

    assert result.draft_version == 2
    assert repository.revisions == {}


@async_test
async def test_repeated_autosave_does_not_increment_draft_twice():
    subject, *_ = service()
    created = await subject.create("策略", context())

    first = await subject.save_draft(
        created.strategy.id,
        source_code="source",
        expected_version=1,
        create_revision=False,
        context=context("same-save"),
    )
    replay = await subject.save_draft(
        created.strategy.id,
        source_code="source",
        expected_version=1,
        create_revision=False,
        context=context("same-save"),
    )

    assert first.draft_version == replay.draft_version == 2


@async_test
async def test_manual_save_creates_immutable_revision():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())

    draft = await subject.save_draft(
        created.strategy.id,
        source_code="print('v1')",
        expected_version=1,
        create_revision=True,
        context=context("save-1"),
    )

    revision = next(iter(repository.revisions.values()))
    assert draft.draft_version == 2
    assert revision.source_code == "print('v1')"
    assert revision.revision_no == 1


@async_test
async def test_rename_uses_draft_version_as_expected_version():
    subject, _, audit, _ = service()
    created = await subject.create("旧名称", context())

    renamed = await subject.rename(
        created.strategy.id,
        name="新名称",
        expected_version=1,
        context=context("rename-1"),
    )

    assert renamed.name == "新名称"
    assert subject._repository.drafts[created.strategy.id].draft_version == 2

    replay = await subject.rename(
        created.strategy.id,
        name="新名称",
        expected_version=1,
        context=context("rename-1"),
    )
    assert replay.name == "新名称"
    assert [item.action_code for item in audit.items].count("strategy.updated") == 1

    with pytest.raises(AppError) as raised:
        await subject.rename(
            created.strategy.id,
            name="并发旧请求",
            expected_version=1,
            context=context("rename-2"),
        )
    assert raised.value.code == "STRATEGY_VERSION_CONFLICT"


@async_test
async def test_save_rejects_stale_expected_version():
    subject, *_ = service()
    created = await subject.create("策略", context())
    await subject.save_draft(
        created.strategy.id,
        source_code="v1",
        expected_version=1,
        create_revision=False,
        context=context("save-1"),
    )

    with pytest.raises(AppError) as raised:
        await subject.save_draft(
            created.strategy.id,
            source_code="stale",
            expected_version=1,
            create_revision=False,
            context=context("save-2"),
        )

    assert raised.value.code == "STRATEGY_VERSION_CONFLICT"
    assert raised.value.status_code == 409


@async_test
async def test_restore_copies_revision_into_new_draft_version():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    await subject.save_draft(
        created.strategy.id,
        source_code="first",
        expected_version=1,
        create_revision=True,
        context=context("save-1"),
    )
    revision_id = next(iter(repository.revisions))
    await subject.save_draft(
        created.strategy.id,
        source_code="second",
        expected_version=2,
        create_revision=False,
        context=context("save-2"),
    )

    restored = await subject.restore_revision(
        created.strategy.id,
        revision_id=revision_id,
        expected_version=3,
        context=context("restore-1"),
    )

    assert restored.source_code == "first"
    assert restored.draft_version == 4


@async_test
async def test_diff_compares_revision_with_current_server_draft():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    await subject.save_draft(
        created.strategy.id,
        source_code="first\n",
        expected_version=1,
        create_revision=True,
        context=context("save-1"),
    )
    revision_id = next(iter(repository.revisions))
    await subject.save_draft(
        created.strategy.id,
        source_code="second\n",
        expected_version=2,
        create_revision=False,
        context=context("save-2"),
    )

    result = await subject.diff(created.strategy.id, revision_id=revision_id)

    assert "-first" in result
    assert "+second" in result


@async_test
async def test_validation_lifecycle_binds_current_draft_and_marks_validated():
    subject, repository, audit, events = service()
    created = await subject.create("策略", context())

    run = await subject.request_validation(
        created.strategy.id,
        metadata={"name": "策略"},
        parameter_schema={"type": "object"},
        params={},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "a" * 64,
        context=context("validate-1"),
    )
    completed = await subject.complete_validation(
        run.id,
        succeeded=True,
        error_code=None,
        evidence_snapshot=checks_for_run(run),
        context=context("validate-complete-1"),
    )

    assert run.strategy_id == created.strategy.id
    assert run.draft_version == 1
    assert completed.status == "SUCCEEDED"
    assert repository.strategies[created.strategy.id].status == "VALIDATED"
    assert audit.items[-1].action_code == "strategy.validation_completed"
    assert events.items[-1].topic == "strategy.validation_completed"


@async_test
async def test_successful_validation_rejects_boolean_or_missing_level_evidence():
    subject, *_ = service()
    created = await subject.create("策略", context())
    run = await subject.request_validation(
        created.strategy.id,
        metadata={"name": "策略"},
        parameter_schema={"type": "object"},
        params={},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "a" * 64,
        context=context("validate-1"),
    )

    with pytest.raises(AppError) as raised:
        await subject.complete_validation(
            run.id,
            succeeded=True,
            error_code=None,
            evidence_snapshot={"all_required_checks_passed": True},
            context=context("complete-1"),
        )

    assert raised.value.code == "STRATEGY_VALIDATION_STALE"


@async_test
async def test_completed_validation_replay_requires_identical_result_and_evidence():
    subject, *_ = service()
    created = await subject.create("策略", context())
    run = await subject.request_validation(
        created.strategy.id,
        metadata={"name": "策略"},
        parameter_schema={"type": "object"},
        params={},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "a" * 64,
        context=context("validate-1"),
    )
    checks = checks_for_run(run)
    await subject.complete_validation(
        run.id,
        succeeded=True,
        error_code=None,
        evidence_snapshot=checks,
        context=context("complete-1"),
    )

    changed = checks_for_run(run)
    with pytest.raises(AppError) as raised:
        await subject.complete_validation(
            run.id,
            succeeded=True,
            error_code=None,
            evidence_snapshot=changed,
            context=context("complete-1"),
        )

    assert raised.value.code == "STRATEGY_IDEMPOTENCY_CONFLICT"


@async_test
async def test_repeated_validation_request_reuses_same_frozen_run():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    values = {
        "metadata": {"name": "策略"},
        "parameter_schema": {"type": "object"},
        "params": {},
        "environment_version": "python-3.12",
        "runner_image_digest": "sha256:" + "a" * 64,
        "context": context("validate-1"),
    }

    first = await subject.request_validation(created.strategy.id, **values)
    replay = await subject.request_validation(created.strategy.id, **values)

    assert replay.id == first.id
    assert len(repository.validation_runs) == 1


@async_test
async def test_late_validation_for_old_draft_does_not_validate_new_draft():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    run = await subject.request_validation(
        created.strategy.id,
        metadata={"name": "策略"},
        parameter_schema={"type": "object"},
        params={},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "a" * 64,
        context=context("validate-1"),
    )
    await subject.save_draft(
        created.strategy.id,
        source_code="changed",
        expected_version=1,
        create_revision=False,
        context=context("save-1"),
    )

    await subject.complete_validation(
        run.id,
        succeeded=True,
        error_code=None,
        evidence_snapshot=checks_for_run(run),
        context=context("validate-complete-1"),
    )

    assert repository.strategies[created.strategy.id].status == "DRAFT"


@async_test
async def test_old_validation_completion_does_not_clobber_new_validation_state():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    values = {
        "metadata": {"name": "策略"},
        "parameter_schema": {"type": "object"},
        "params": {},
        "environment_version": "python-3.12",
        "runner_image_digest": "sha256:" + "a" * 64,
    }
    old = await subject.request_validation(
        created.strategy.id, **values, context=context("validate-1")
    )
    await subject.save_draft(
        created.strategy.id,
        source_code="changed",
        expected_version=1,
        create_revision=False,
        context=context("save-1"),
    )
    await subject.request_validation(
        created.strategy.id, **values, context=context("validate-2")
    )

    await subject.complete_validation(
        old.id,
        succeeded=True,
        error_code=None,
        evidence_snapshot=checks_for_run(old),
        context=context("validate-complete-1"),
    )

    assert repository.strategies[created.strategy.id].status == "VALIDATING"


@async_test
async def test_publish_freezes_current_source_and_reuses_failed_snapshot():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    draft = await subject.save_draft(
        created.strategy.id,
        source_code="def calculate_targets(history, params, context):\n    return {}\n",
        expected_version=1,
        create_revision=True,
        context=context("save-1"),
    )
    validation_id, validation = succeeded_validation(
        created.strategy.id, draft.draft_version, subject.hash_source(draft.source_code)
    )
    repository.validation_runs[validation_id] = validation
    repository.strategies[created.strategy.id].status = "VALIDATED"
    evidence = publish_evidence(validation, draft.draft_version)

    frozen = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-1")
    )
    in_progress_replay = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-1")
    )
    assert in_progress_replay.version.id == frozen.version.id
    await subject.fail_publish(
        created.strategy.id,
        frozen.version.id,
        "GIT_FAILED",
        context=context("pub-failed-1"),
    )
    failed_replay = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-1")
    )
    assert failed_replay.version.status == "PUBLISH_FAILED"
    assert failed_replay.replayed is True
    retried = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-2")
    )

    assert retried.version.id == frozen.version.id
    assert retried.version.source_code == draft.source_code
    assert retried.version.strategy_metadata == validation.evidence_snapshot["metadata"]
    assert retried.version.status == "PUBLISHING"


@async_test
async def test_publish_failure_is_audited_and_emitted():
    subject, repository, audit, events = service()
    created = await subject.create("策略", context())
    validation_id, validation = succeeded_validation(
        created.strategy.id, 1, subject.hash_source("")
    )
    repository.validation_runs[validation_id] = validation
    repository.strategies[created.strategy.id].status = "VALIDATED"
    evidence = publish_evidence(validation, 1)
    frozen = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-1")
    )

    failed = await subject.fail_publish(
        created.strategy.id,
        frozen.version.id,
        "STRATEGY_GIT_FAILED",
        context=context("pub-failed-1"),
    )

    assert failed.status == "PUBLISH_FAILED"
    assert audit.items[-1].action_code == "strategy.publish_failed"
    assert events.items[-1].topic == "strategy.publish_failed"


@async_test
async def test_failure_confirmation_converges_already_published_run_to_success():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    validation_id, validation = succeeded_validation(
        created.strategy.id, 1, subject.hash_source("")
    )
    repository.validation_runs[validation_id] = validation
    repository.strategies[created.strategy.id].status = "VALIDATED"
    frozen = await subject.begin_publish(
        created.strategy.id,
        publish_evidence(validation, 1),
        context("pub-1"),
    )
    await subject.claim_publish_run(frozen.run.id)
    await subject.complete_publish_run(
        frozen.run.id,
        git_commit="a" * 40,
        context=context("pub-worker-1"),
    )

    version = await subject.fail_publish_run(
        frozen.run.id,
        "STRATEGY_DATABASE_FAILED",
        context=context("pub-failed-1"),
    )

    assert version.status == "PUBLISHED"
    assert repository.runs[frozen.run.id].status == "SUCCEEDED"


@async_test
async def test_publish_rejects_stale_validation_evidence():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    validation_id, validation = succeeded_validation(
        created.strategy.id, 1, subject.hash_source("")
    )
    repository.validation_runs[validation_id] = validation
    await subject.save_draft(
        created.strategy.id,
        source_code="changed",
        expected_version=1,
        create_revision=False,
        context=context("save-1"),
    )
    repository.strategies[created.strategy.id].status = "VALIDATED"
    evidence = publish_evidence(validation, 2)

    with pytest.raises(AppError) as raised:
        await subject.begin_publish(created.strategy.id, evidence, context("pub-1"))

    assert raised.value.code == "STRATEGY_VALIDATION_STALE"


@async_test
async def test_publish_rejects_validation_from_another_strategy_or_draft():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    current_hash = subject.hash_source("")
    validation_id, validation = succeeded_validation(uuid4(), 99, current_hash)
    repository.validation_runs[validation_id] = validation
    repository.strategies[created.strategy.id].status = "VALIDATED"
    evidence = publish_evidence(validation, 1)

    with pytest.raises(AppError) as raised:
        await subject.begin_publish(created.strategy.id, evidence, context("pub-1"))

    assert raised.value.code == "STRATEGY_VALIDATION_STALE"
    assert repository.versions == {}


@async_test
async def test_publish_rejects_non_json_or_non_finite_snapshots():
    subject, *_ = service()
    created = await subject.create("策略", context())
    with pytest.raises(AppError) as raised:
        await subject.request_validation(
            created.strategy.id,
            metadata={"bad": float("nan")},
            parameter_schema={"type": "object"},
            params={},
            environment_version="python-3.12",
            runner_image_digest="sha256:" + "a" * 64,
            context=context("validate-1"),
        )

    assert raised.value.code == "STRATEGY_INPUT_INVALID"


@async_test
async def test_complete_publish_makes_version_bindable_and_archive_blocks_edits():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    draft = await subject.save_draft(
        created.strategy.id,
        source_code="source",
        expected_version=1,
        create_revision=True,
        context=context("save-1"),
    )
    validation_id, validation = succeeded_validation(
        created.strategy.id, draft.draft_version, subject.hash_source(draft.source_code)
    )
    repository.validation_runs[validation_id] = validation
    repository.strategies[created.strategy.id].status = "VALIDATED"
    evidence = publish_evidence(validation, draft.draft_version)
    frozen = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-1")
    )

    published = await subject.complete_publish(
        created.strategy.id,
        frozen.version.id,
        git_commit="a" * 40,
        context=context("pub-1"),
    )
    await subject.archive(
        created.strategy.id, expected_version=2, context=context("archive-1")
    )

    assert published.status == "ARCHIVED"
    assert published.published_at == datetime(2026, 7, 21, tzinfo=UTC)
    with pytest.raises(AppError) as raised:
        await subject.save_draft(
            created.strategy.id,
            source_code="changed",
            expected_version=2,
            create_revision=False,
            context=context("save-2"),
        )
    assert raised.value.code == "STRATEGY_ARCHIVED"

    replay = await subject.archive(
        created.strategy.id, expected_version=2, context=context("archive-1")
    )
    assert replay.status == "ARCHIVED"

    restored = await subject.restore(
        created.strategy.id,
        expected_version=2,
        context=context("restore-1"),
    )
    assert restored.status == "PUBLISHED"


@async_test
async def test_list_is_paginated_and_archived_is_opt_in():
    subject, repository, *_ = service()
    await subject.create("策略一", context("create-1"))
    second = await subject.create("策略二", context("create-2"))
    repository.strategies[second.strategy.id].status = "ARCHIVED"

    rows, total = await subject.list(page=1, page_size=20, include_archived=False)

    assert total == 1
    assert [row.name for row in rows] == ["策略一"]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(
        "long_invest.modules.strategies.service._utc_now",
        lambda: datetime(2026, 7, 21, tzinfo=UTC),
    )
