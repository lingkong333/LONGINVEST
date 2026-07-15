from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from long_invest.modules.market_data.contracts import (
    QualityIssueStatus,
    QualityResolutionAction,
    QualitySeverity,
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
