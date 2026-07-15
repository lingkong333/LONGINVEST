from hashlib import sha256
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.market_data.contracts import (
    QualityIssueStatus,
    QualityResolutionAction,
    QualitySeverity,
    RequestQualityRefetch,
)
from long_invest.modules.market_data.models import DataQualityIssue
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class QualityEventPort(Protocol):
    session: AsyncSession

    async def append_resolved(self, issue: DataQualityIssue) -> None: ...

    async def append_refetch_requested(
        self,
        issue: DataQualityIssue,
        command: RequestQualityRefetch,
    ) -> None: ...


class TransactionBoundOutboxWriter(Protocol):
    async def append(
        self,
        *,
        session: AsyncSession,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        queue: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None: ...


class TransactionalQualityEventAdapter(QualityEventPort):
    def __init__(
        self,
        session: AsyncSession,
        writer: TransactionBoundOutboxWriter | None = None,
    ) -> None:
        self.session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def append_resolved(self, issue: DataQualityIssue) -> None:
        status = QualityIssueStatus(issue.status).value
        severity = QualitySeverity(issue.severity).value
        action = QualityResolutionAction(issue.resolution_action).value
        if issue.resolved_at is None or issue.resolved_by_user_id is None:
            raise ValueError("resolved quality issue is missing resolution metadata")

        await self._writer.append(
            session=self.session,
            topic="data_quality_issue.resolved",
            aggregate_type="data_quality_issue",
            aggregate_id=str(issue.id),
            queue="domain-events",
            payload={
                "event_type": "data_quality_issue.resolved",
                "issue_id": str(issue.id),
                "issue_type": issue.issue_type,
                "subject_type": issue.subject_type,
                "subject_id": issue.subject_id,
                "symbol": issue.symbol,
                "status": status,
                "severity": severity,
                "resolution_action": action,
                "selected_source": issue.selected_source,
                "resolved_by_user_id": issue.resolved_by_user_id,
                "resolved_at": issue.resolved_at.isoformat(),
            },
            dedupe_key=f"quality:{issue.id}:{status}:{action}",
        )

    async def append_refetch_requested(
        self,
        issue: DataQualityIssue,
        command: RequestQualityRefetch,
    ) -> None:
        status = QualityIssueStatus(issue.status).value
        key_hash = sha256(command.idempotency_key.encode("utf-8")).hexdigest()
        await self._writer.append(
            session=self.session,
            topic="data_quality_issue.refetch_requested",
            aggregate_type="data_quality_issue",
            aggregate_id=str(issue.id),
            queue="domain-events",
            payload={
                "event_type": "data_quality_issue.refetch_requested",
                "issue_id": str(issue.id),
                "issue_type": issue.issue_type,
                "subject_type": issue.subject_type,
                "subject_id": issue.subject_id,
                "symbol": issue.symbol,
                "status": status,
                "requested_by": command.actor_user_id,
                "reason": command.reason,
            },
            dedupe_key=f"quality-refetch:{key_hash}",
        )
