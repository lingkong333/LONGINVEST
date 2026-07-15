from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from long_invest.modules.market_data.contracts import (
    QualityIssueStatus,
    QualityResolutionAction,
    QualitySeverity,
    RequestQualityRefetch,
)
from long_invest.modules.market_data.integrations import (
    TransactionalQualityEventAdapter,
)
from long_invest.modules.market_data.models import DataQualityIssue
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def refetch_issue(issue_id: UUID | None = None) -> DataQualityIssue:
    return DataQualityIssue(
        id=issue_id or uuid4(),
        issue_type="QUOTE_CONFLICT",
        subject_type="quote_cycle_item",
        subject_id="item-1",
        symbol="600000.SH",
        status=QualityIssueStatus.OPEN,
        severity=QualitySeverity.WARNING,
        evidence={"secret": {"provider_url": "https://example.invalid"}},
        dedupe_key=f"quote:{issue_id or uuid4()}:conflict",
        occurrence_count=1,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def refetch_command(issue_id: UUID, **overrides: str) -> RequestQualityRefetch:
    values = {
        "actor_user_id": "user-1",
        "reason": "retry provider",
        "idempotency_key": "client-refetch-request-1",
    }
    values.update(overrides)
    return RequestQualityRefetch(issue_id=issue_id, **values)


def outbox_event(request_hash: str) -> Mock:
    event = Mock()
    event.payload = {"request_hash": request_hash}
    return event


@pytest.mark.anyio
async def test_event_adapter_writes_only_stable_resolution_fields() -> None:
    session = Mock()
    writer = Mock()
    writer.append = AsyncMock()
    issue = DataQualityIssue(
        id=uuid4(),
        issue_type="QUOTE_CONFLICT",
        subject_type="quote_cycle_item",
        subject_id="item-1",
        symbol="600000.SH",
        status=QualityIssueStatus.RESOLVED,
        severity=QualitySeverity.WARNING,
        evidence={"sources": {"EASTMONEY": {"price": "10.00"}}},
        dedupe_key="quote:item-1:conflict",
        occurrence_count=1,
        first_seen_at=NOW,
        last_seen_at=NOW,
        resolved_at=NOW,
        resolved_by_user_id="user-1",
        resolution_action=QualityResolutionAction.SELECT_SOURCE,
        resolution_reason="checked",
        selected_source="EASTMONEY",
    )

    await TransactionalQualityEventAdapter(session, writer=writer).append_resolved(
        issue
    )

    arguments = writer.append.await_args.kwargs
    assert arguments == {
        "session": session,
        "topic": "data_quality_issue.resolved",
        "aggregate_type": "data_quality_issue",
        "aggregate_id": str(issue.id),
        "queue": "domain-events",
        "payload": {
            "event_type": "data_quality_issue.resolved",
            "issue_id": str(issue.id),
            "issue_type": "QUOTE_CONFLICT",
            "subject_type": "quote_cycle_item",
            "subject_id": "item-1",
            "symbol": "600000.SH",
            "status": "RESOLVED",
            "severity": "WARNING",
            "resolution_action": "SELECT_SOURCE",
            "selected_source": "EASTMONEY",
            "resolved_by_user_id": "user-1",
            "resolved_at": NOW.isoformat(),
        },
        "dedupe_key": f"quality:{issue.id}:RESOLVED:SELECT_SOURCE",
    }
    assert "evidence" not in arguments["payload"]
    assert len(arguments["dedupe_key"]) <= 200


@pytest.mark.anyio
async def test_event_adapter_writes_stable_refetch_request_without_evidence() -> None:
    session = Mock()
    session.scalar = AsyncMock()
    writer = Mock()
    writer.append = AsyncMock()
    issue = refetch_issue(UUID("00000000-0000-0000-0000-000000000001"))
    command = refetch_command(issue.id)
    request_hash = "d2b7eedc0078524c7ff10cb3cd3d9a0f811425a28effbe78dbef45877c3a6639"
    session.scalar.side_effect = [None, outbox_event(request_hash)]
    adapter = TransactionalQualityEventAdapter(session, writer=writer)

    await adapter.append_refetch_requested(issue, command)
    arguments = writer.append.await_args.kwargs

    assert arguments == {
        "session": session,
        "topic": "data_quality_issue.refetch_requested",
        "aggregate_type": "data_quality_issue",
        "aggregate_id": str(issue.id),
        "queue": "domain-events",
        "payload": {
            "event_type": "data_quality_issue.refetch_requested",
            "issue_id": str(issue.id),
            "issue_type": "QUOTE_CONFLICT",
            "subject_type": "quote_cycle_item",
            "subject_id": "item-1",
            "symbol": "600000.SH",
            "status": "OPEN",
            "requested_by": "user-1",
            "reason": "retry provider",
            "request_hash": request_hash,
        },
        "dedupe_key": (
            "quality-refetch:00000000-0000-0000-0000-000000000001:"
            "1d1f693682aafaa3769dd0d98d6fd775087e2dd3dd099292e989d83d80907cdc"
        ),
    }
    assert "evidence" not in arguments["payload"]
    assert "provider_url" not in str(arguments)
    assert command.idempotency_key not in arguments["dedupe_key"]
    assert len(arguments["dedupe_key"]) < 200


@pytest.mark.anyio
async def test_refetch_same_issue_same_key_and_content_replays_without_append() -> None:
    issue = refetch_issue()
    command = refetch_command(issue.id)
    session = Mock()
    session.scalar = AsyncMock(side_effect=[None, None])
    writer = Mock()
    writer.append = AsyncMock()
    adapter = TransactionalQualityEventAdapter(session, writer=writer)

    await adapter.append_refetch_requested(issue, command)
    request_hash = writer.append.await_args.kwargs["payload"]["request_hash"]
    session.scalar.side_effect = [outbox_event(request_hash)]
    await adapter.append_refetch_requested(issue, command)

    writer.append.assert_awaited_once()


@pytest.mark.anyio
async def test_refetch_same_client_key_is_scoped_to_each_issue() -> None:
    first_issue = refetch_issue()
    second_issue = refetch_issue()
    session = Mock()
    session.scalar = AsyncMock(side_effect=[None, None, None, None])
    writer = Mock()
    writer.append = AsyncMock()
    adapter = TransactionalQualityEventAdapter(session, writer=writer)

    await adapter.append_refetch_requested(first_issue, refetch_command(first_issue.id))
    first = writer.append.await_args.kwargs
    await adapter.append_refetch_requested(
        second_issue, refetch_command(second_issue.id)
    )
    second = writer.append.await_args.kwargs

    assert writer.append.await_count == 2
    assert first["dedupe_key"] != second["dedupe_key"]
    assert str(first_issue.id) in first["dedupe_key"]
    assert str(second_issue.id) in second["dedupe_key"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [{"actor_user_id": "user-2"}, {"reason": "different reason"}],
)
async def test_refetch_same_issue_and_key_rejects_different_request(overrides) -> None:
    issue = refetch_issue()
    original = refetch_command(issue.id)
    changed = refetch_command(issue.id, **overrides)
    session = Mock()
    session.scalar = AsyncMock(side_effect=[None, None])
    writer = Mock()
    writer.append = AsyncMock()
    adapter = TransactionalQualityEventAdapter(session, writer=writer)

    await adapter.append_refetch_requested(issue, original)
    request_hash = writer.append.await_args.kwargs["payload"]["request_hash"]
    session.scalar.side_effect = [outbox_event(request_hash)]

    with pytest.raises(AppError) as caught:
        await adapter.append_refetch_requested(issue, changed)

    assert caught.value.code == "IDEMPOTENCY_KEY_CONFLICT"
    assert caught.value.status_code == 409
    writer.append.assert_awaited_once()
