import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from long_invest.modules.market_data.contracts import (
    OpenQualityIssue,
    QualityIssueStatus,
    QualityResolutionAction,
    QualitySeverity,
    ResolveQualityIssue,
)
from long_invest.modules.market_data.models import DataQualityIssue
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import QualityIssueService
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def open_command(**overrides: object) -> OpenQualityIssue:
    values = {
        "issue_type": "QUOTE_CONFLICT",
        "subject_type": "quote_cycle_item",
        "subject_id": "item-1",
        "symbol": "600000.SH",
        "severity": QualitySeverity.WARNING,
        "evidence": {
            "sources": {
                "EASTMONEY": {"price": "10.00"},
                "SINA": {"price": "10.10"},
            }
        },
        "dedupe_key": "quote:item-1:conflict",
    }
    values.update(overrides)
    return OpenQualityIssue(**values)  # type: ignore[arg-type]


def resolve_command(issue_id, **overrides: object) -> ResolveQualityIssue:
    values = {
        "issue_id": issue_id,
        "action": QualityResolutionAction.RESOLVE,
        "actor_user_id": "user-1",
        "reason": "evidence checked",
    }
    values.update(overrides)
    return ResolveQualityIssue(**values)  # type: ignore[arg-type]


class MemoryRepository:
    def __init__(self) -> None:
        self.records: dict[object, DataQualityIssue] = {}
        self.flush_error: Exception | None = None
        self.concurrent_record: DataQualityIssue | None = None
        self.flush_calls = 0

    async def find_by_dedupe_key(self, key: str):
        return next(
            (record for record in self.records.values() if record.dedupe_key == key),
            None,
        )

    async def get_for_update(self, issue_id):
        return self.records.get(issue_id)

    async def claim_issue(self, record):
        if self.concurrent_record is not None:
            concurrent = self.concurrent_record
            self.records[concurrent.id] = concurrent
            return concurrent, False
        self.records[record.id] = record
        await self.flush()
        return record, True

    async def flush(self):
        self.flush_calls += 1
        if self.flush_error is not None:
            raise self.flush_error


class ConcurrentRepository:
    def __init__(self) -> None:
        self.records: dict[str, DataQualityIssue] = {}
        self.find_barrier = asyncio.Barrier(2)
        self.claim_barrier = asyncio.Barrier(2)
        self.claim_lock = asyncio.Lock()
        self.update_lock = asyncio.Lock()
        self.update_owner: asyncio.Task[object] | None = None
        self.update_lock_count = 0

    async def find_by_dedupe_key(self, key: str):
        await self.find_barrier.wait()
        return self.records.get(key)

    async def claim_issue(self, record: DataQualityIssue):
        await self.claim_barrier.wait()
        async with self.claim_lock:
            existing = self.records.get(record.dedupe_key)
            if existing is not None:
                return existing, False
            self.records[record.dedupe_key] = record
            return record, True

    async def get_for_update(self, issue_id):
        await self.update_lock.acquire()
        self.update_owner = asyncio.current_task()
        self.update_lock_count += 1
        return next(
            (record for record in self.records.values() if record.id == issue_id),
            None,
        )

    async def flush(self):
        if self.update_owner is asyncio.current_task():
            self.update_owner = None
            self.update_lock.release()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("requires_review", "expected_status"),
    [
        (False, QualityIssueStatus.OPEN),
        (True, QualityIssueStatus.REVIEW_REQUIRED),
    ],
)
async def test_open_creates_initial_status_and_json_evidence(
    requires_review: bool,
    expected_status: QualityIssueStatus,
) -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)

    result = await service.open(open_command(requires_review=requires_review))

    assert result.created is True
    assert result.replayed is False
    assert result.issue.status == expected_status
    assert result.issue.occurrence_count == 1
    assert result.issue.first_seen_at == NOW
    assert result.issue.last_seen_at == NOW
    assert type(result.issue.evidence) is dict
    assert type(result.issue.evidence["sources"]) is dict


@pytest.mark.anyio
async def test_open_updates_active_duplicate_and_only_upgrades_severity() -> None:
    repository = MemoryRepository()
    times = iter((NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)))
    service = QualityIssueService(repository, now_provider=lambda: next(times))
    created = await service.open(open_command(severity=QualitySeverity.WARNING))

    upgraded = await service.open(
        open_command(
            severity=QualitySeverity.CRITICAL,
            evidence={"latest": 2},
        )
    )
    not_downgraded = await service.open(
        open_command(
            severity=QualitySeverity.INFO,
            evidence={"latest": 3},
        )
    )

    assert upgraded.issue is created.issue
    assert not_downgraded.created is False
    assert not_downgraded.replayed is True
    assert created.issue.occurrence_count == 3
    assert created.issue.last_seen_at == NOW + timedelta(minutes=2)
    assert created.issue.severity == QualitySeverity.CRITICAL
    assert created.issue.evidence == {"latest": 3}


@pytest.mark.anyio
async def test_open_terminal_duplicate_is_replayed_without_mutation() -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    created = await service.open(open_command())
    await service.resolve(resolve_command(created.issue.id))
    before = (
        created.issue.occurrence_count,
        created.issue.last_seen_at,
        created.issue.severity,
        created.issue.evidence,
    )

    replay = await service.open(
        open_command(
            severity=QualitySeverity.CRITICAL,
            evidence={"latest": "must-not-replace"},
        )
    )

    assert replay.created is False
    assert replay.replayed is True
    assert replay.issue is created.issue
    assert (
        replay.issue.occurrence_count,
        replay.issue.last_seen_at,
        replay.issue.severity,
        replay.issue.evidence,
    ) == before


@pytest.mark.anyio
async def test_open_concurrent_claim_uses_winning_record_as_replay() -> None:
    repository = MemoryRepository()
    winner = DataQualityIssue(
        issue_type="QUOTE_CONFLICT",
        subject_type="quote_cycle_item",
        subject_id="item-1",
        symbol="600000.SH",
        status=QualityIssueStatus.RESOLVED,
        severity=QualitySeverity.ERROR,
        evidence={"winner": True},
        dedupe_key="quote:item-1:conflict",
        occurrence_count=1,
        first_seen_at=NOW,
        last_seen_at=NOW,
        resolved_at=NOW,
        resolved_by_user_id="user-1",
        resolution_action=QualityResolutionAction.RESOLVE,
        resolution_reason="done",
    )
    repository.concurrent_record = winner

    result = await QualityIssueService(repository, now_provider=lambda: NOW).open(
        open_command()
    )

    assert result.issue is winner
    assert result.created is False
    assert result.replayed is True


@pytest.mark.anyio
async def test_simultaneous_open_claims_one_issue_and_serializes_replay() -> None:
    repository = ConcurrentRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    commands = (
        open_command(
            severity=QualitySeverity.WARNING,
            evidence={"request": "warning"},
        ),
        open_command(
            severity=QualitySeverity.CRITICAL,
            evidence={"request": "critical"},
        ),
    )

    results = await asyncio.gather(*(service.open(command) for command in commands))

    assert len({result.issue.id for result in results}) == 1
    assert sorted(result.created for result in results) == [False, True]
    stored = next(iter(repository.records.values()))
    replay_index = next(
        index for index, result in enumerate(results) if result.created is False
    )
    assert stored.occurrence_count == 2
    assert stored.severity == QualitySeverity.CRITICAL
    assert stored.evidence == commands[replay_index].evidence
    assert repository.update_lock_count == 1


@pytest.mark.anyio
async def test_open_does_not_report_success_when_flush_fails() -> None:
    repository = MemoryRepository()
    repository.flush_error = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await QualityIssueService(repository, now_provider=lambda: NOW).open(
            open_command()
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        (QualityResolutionAction.RESOLVE, QualityIssueStatus.RESOLVED),
        (QualityResolutionAction.INVALIDATE, QualityIssueStatus.INVALIDATED),
        (QualityResolutionAction.SELECT_SOURCE, QualityIssueStatus.RESOLVED),
    ],
)
async def test_resolve_applies_supported_terminal_transitions(
    action: QualityResolutionAction,
    expected_status: QualityIssueStatus,
) -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command(requires_review=True))
    selected_source = (
        "SINA" if action is QualityResolutionAction.SELECT_SOURCE else None
    )

    result = await service.resolve(
        resolve_command(
            opened.issue.id,
            action=action,
            selected_source=selected_source,
        )
    )

    assert result.replayed is False
    assert result.issue.status == expected_status
    assert result.issue.resolved_at == NOW
    assert result.issue.resolved_by_user_id == "user-1"
    assert result.issue.resolution_action == action
    assert result.issue.resolution_reason == "evidence checked"
    assert result.issue.selected_source == selected_source


@pytest.mark.anyio
async def test_resolve_identical_terminal_decision_is_idempotent() -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command())
    command = resolve_command(opened.issue.id)
    first = await service.resolve(command)
    flush_calls = repository.flush_calls

    replay = await service.resolve(command)

    assert replay.issue is first.issue
    assert replay.replayed is True
    assert repository.flush_calls == flush_calls


@pytest.mark.anyio
async def test_resolve_rejects_a_different_terminal_decision() -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command())
    await service.resolve(resolve_command(opened.issue.id))

    with pytest.raises(AppError) as caught:
        await service.resolve(
            resolve_command(
                opened.issue.id,
                action=QualityResolutionAction.INVALIDATE,
            )
        )

    assert caught.value.code == "QUALITY_ISSUE_STATE_CONFLICT"
    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_resolve_missing_issue_is_404() -> None:
    with pytest.raises(AppError) as caught:
        await QualityIssueService(MemoryRepository(), now_provider=lambda: NOW).resolve(
            resolve_command(uuid4())
        )

    assert caught.value.code == "QUALITY_ISSUE_NOT_FOUND"
    assert caught.value.status_code == 404


@pytest.mark.anyio
async def test_select_source_rejects_source_not_present_in_evidence() -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command())

    with pytest.raises(AppError) as caught:
        await service.resolve(
            resolve_command(
                opened.issue.id,
                action=QualityResolutionAction.SELECT_SOURCE,
                selected_source="TENCENT",
            )
        )

    assert caught.value.code == "QUALITY_SOURCE_NOT_AVAILABLE"
    assert caught.value.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize(
    "evidence",
    [
        {"sources": ["EASTMONEY", "SINA"]},
        {"sources": "EASTMONEY"},
        {"other": {}},
    ],
)
async def test_select_source_rejects_invalid_sources_shape(evidence) -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command(evidence=evidence))

    with pytest.raises(AppError) as caught:
        await service.resolve(
            resolve_command(
                opened.issue.id,
                action=QualityResolutionAction.SELECT_SOURCE,
                selected_source="SINA",
            )
        )

    assert caught.value.code == "QUALITY_EVIDENCE_INVALID"
    assert caught.value.status_code == 422


@pytest.mark.anyio
async def test_select_source_rejects_non_object_evidence_with_stable_error() -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command())
    opened.issue.evidence = ["invalid"]  # type: ignore[assignment]

    with pytest.raises(AppError) as caught:
        await service.resolve(
            resolve_command(
                opened.issue.id,
                action=QualityResolutionAction.SELECT_SOURCE,
                selected_source="SINA",
            )
        )

    assert caught.value.code == "QUALITY_EVIDENCE_INVALID"
    assert caught.value.status_code == 422


@pytest.mark.anyio
async def test_resolve_rejects_invalid_persisted_status_with_stable_error() -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command())
    opened.issue.status = "BROKEN"

    with pytest.raises(AppError) as caught:
        await service.resolve(resolve_command(opened.issue.id))

    assert caught.value.code == "QUALITY_ISSUE_STATE_INVALID"
    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_resolve_rejects_invalid_action_with_stable_error() -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command())
    command = resolve_command(opened.issue.id)
    object.__setattr__(command, "action", "EDIT_PRICE")

    with pytest.raises(AppError) as caught:
        await service.resolve(command)

    assert caught.value.code == "QUALITY_ACTION_NOT_ALLOWED"
    assert caught.value.status_code == 422


@pytest.mark.anyio
async def test_refetch_is_not_a_terminal_resolution_action() -> None:
    repository = MemoryRepository()
    service = QualityIssueService(repository, now_provider=lambda: NOW)
    opened = await service.open(open_command())

    with pytest.raises(AppError) as caught:
        await service.resolve(
            resolve_command(
                opened.issue.id,
                action=QualityResolutionAction.REFETCH,
            )
        )

    assert caught.value.code == "QUALITY_ACTION_NOT_ALLOWED"
    assert caught.value.status_code == 422
    assert opened.issue.status == QualityIssueStatus.OPEN


@pytest.mark.anyio
async def test_repository_claim_conflict_rereads_without_transaction_rollback() -> None:
    session = Mock()
    session.flush = AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("duplicate"))
    )
    session.scalar = AsyncMock()
    nested = AsyncMock()
    session.begin_nested.return_value = nested
    existing = Mock(spec=DataQualityIssue)
    session.scalar.return_value = existing
    candidate = Mock(spec=DataQualityIssue)
    candidate.dedupe_key = "same-key"

    claimed, created = await QualityIssueRepository(session).claim_issue(candidate)

    assert claimed is existing
    assert created is False
    session.begin_nested.assert_called_once_with()
    session.add.assert_called_once_with(candidate)
    session.flush.assert_awaited_once_with()
    session.rollback.assert_not_called()
    nested.__aenter__.assert_awaited_once()
    nested.__aexit__.assert_awaited_once()
    exit_args = nested.__aexit__.await_args.args
    assert exit_args[0] is IntegrityError
    assert isinstance(exit_args[1], IntegrityError)
    reread = session.scalar.await_args.args[0]
    compiled = reread.compile(dialect=postgresql.dialect())
    assert "data_quality_issue.dedupe_key" in str(compiled)
    assert "same-key" in compiled.params.values()


@pytest.mark.anyio
async def test_repository_get_for_update_uses_postgresql_row_lock() -> None:
    session = AsyncMock()
    issue_id = uuid4()

    await QualityIssueRepository(session).get_for_update(issue_id)

    statement = session.scalar.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "FOR UPDATE" in str(compiled).upper()
    assert issue_id in compiled.params.values()


@pytest.mark.anyio
async def test_repository_list_and_count_apply_filters_and_stable_pagination() -> None:
    session = AsyncMock()
    rows = Mock()
    rows.all.return_value = []
    session.scalars.return_value = rows
    session.scalar.return_value = 7
    repository = QualityIssueRepository(session)

    listed = await repository.list(
        status=QualityIssueStatus.OPEN,
        issue_type="QUOTE_CONFLICT",
        symbol="600000.SH",
        page=2,
        page_size=5,
    )
    count = await repository.count(
        status=QualityIssueStatus.OPEN,
        issue_type="QUOTE_CONFLICT",
        symbol="600000.SH",
    )

    statement = session.scalars.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "data_quality_issue.status" in sql
    assert "data_quality_issue.issue_type" in sql
    assert "data_quality_issue.symbol" in sql
    assert "ORDER BY data_quality_issue.last_seen_at DESC, data_quality_issue.id" in sql
    assert compiled.params["param_1"] == 5
    assert compiled.params["param_2"] == 5
    count_sql = str(
        session.scalar.await_args.args[0].compile(dialect=postgresql.dialect())
    )
    assert "count(*)" in count_sql
    assert listed == []
    assert count == 7
