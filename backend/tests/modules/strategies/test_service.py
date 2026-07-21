from __future__ import annotations

import asyncio
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

    async def get_validation_run(self, validation_run_id):
        return self.validation_runs.get(validation_run_id)

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

    async def get_version(self, strategy_id, version_id, *, for_update=False):
        version = self.versions.get(version_id)
        return version if version and version.strategy_id == strategy_id else None

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

    replay = await subject.rename(
        created.strategy.id,
        name="新名称",
        expected_version=1,
        context=context("rename-1"),
    )
    assert replay.name == "新名称"
    assert [item.action_code for item in audit.items].count("strategy.updated") == 1


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
    validation_id = uuid4()
    repository.validation_runs[validation_id] = SimpleNamespace(
        id=validation_id, status="SUCCEEDED", error_code=None
    )
    evidence = PublishEvidence(
        validation_run_id=validation_id,
        source_code_hash=subject.hash_source(draft.source_code),
        metadata={"name": "策略"},
        parameter_schema={"type": "object"},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "a" * 64,
    )

    frozen = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-1")
    )
    await subject.fail_publish(
        created.strategy.id,
        frozen.version.id,
        "GIT_FAILED",
        context=context("pub-failed-1"),
    )
    retried = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-1")
    )

    assert retried.version.id == frozen.version.id
    assert retried.version.source_code == draft.source_code
    assert retried.version.status == "PUBLISHING"


@async_test
async def test_publish_failure_is_audited_and_emitted():
    subject, repository, audit, events = service()
    created = await subject.create("策略", context())
    validation_id = uuid4()
    repository.validation_runs[validation_id] = SimpleNamespace(
        id=validation_id, status="SUCCEEDED", error_code=None
    )
    evidence = PublishEvidence(
        validation_run_id=validation_id,
        source_code_hash=subject.hash_source(""),
        metadata={},
        parameter_schema={"type": "object"},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "a" * 64,
    )
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
async def test_publish_rejects_stale_validation_evidence():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    validation_id = uuid4()
    repository.validation_runs[validation_id] = SimpleNamespace(
        id=validation_id, status="SUCCEEDED", error_code=None
    )
    evidence = PublishEvidence(
        validation_run_id=validation_id,
        source_code_hash="0" * 64,
        metadata={},
        parameter_schema={"type": "object"},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "a" * 64,
    )

    with pytest.raises(AppError) as raised:
        await subject.begin_publish(created.strategy.id, evidence, context("pub-1"))

    assert raised.value.code == "STRATEGY_VALIDATION_STALE"


@async_test
async def test_publish_rejects_non_json_or_non_finite_snapshots():
    subject, repository, *_ = service()
    created = await subject.create("策略", context())
    validation_id = uuid4()
    repository.validation_runs[validation_id] = SimpleNamespace(
        id=validation_id, status="SUCCEEDED", error_code=None
    )
    evidence = PublishEvidence(
        validation_run_id=validation_id,
        source_code_hash=subject.hash_source(""),
        metadata={"bad": float("nan")},
        parameter_schema={"type": "object"},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "a" * 64,
    )

    with pytest.raises(AppError) as raised:
        await subject.begin_publish(created.strategy.id, evidence, context("pub-1"))

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
    validation_id = uuid4()
    repository.validation_runs[validation_id] = SimpleNamespace(
        id=validation_id, status="SUCCEEDED", error_code=None
    )
    evidence = PublishEvidence(
        validation_run_id=validation_id,
        source_code_hash=subject.hash_source(draft.source_code),
        metadata={},
        parameter_schema={"type": "object"},
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "b" * 64,
    )
    frozen = await subject.begin_publish(
        created.strategy.id, evidence, context("pub-1")
    )

    published = await subject.complete_publish(
        created.strategy.id,
        frozen.version.id,
        git_commit="abcdef1234567",
        context=context("pub-1"),
    )
    await subject.archive(
        created.strategy.id, expected_version=2, context=context("archive-1")
    )

    assert published.status == "PUBLISHED"
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
