from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

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

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


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
    writer = Mock()
    writer.append = AsyncMock()
    issue = DataQualityIssue(
        id=uuid4(),
        issue_type="QUOTE_CONFLICT",
        subject_type="quote_cycle_item",
        subject_id="item-1",
        symbol="600000.SH",
        status=QualityIssueStatus.OPEN,
        severity=QualitySeverity.WARNING,
        evidence={"secret": {"provider_url": "https://example.invalid"}},
        dedupe_key="quote:item-1:conflict",
        occurrence_count=1,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    command = RequestQualityRefetch(
        issue_id=issue.id,
        actor_user_id="user-1",
        reason="retry provider",
        idempotency_key="client-refetch-request-1",
    )
    adapter = TransactionalQualityEventAdapter(session, writer=writer)

    await adapter.append_refetch_requested(issue, command)
    first = writer.append.await_args.kwargs
    await adapter.append_refetch_requested(issue, command)
    second = writer.append.await_args.kwargs

    assert first == second
    assert first == {
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
        },
        "dedupe_key": (
            "quality-refetch:"
            "1d1f693682aafaa3769dd0d98d6fd775087e2dd3dd099292e989d83d80907cdc"
        ),
    }
    assert "evidence" not in first["payload"]
    assert "provider_url" not in str(first)
    assert len(first["dedupe_key"]) <= 200
