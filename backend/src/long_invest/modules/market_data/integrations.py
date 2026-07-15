import json
from hashlib import sha256
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.market_data.contracts import (
    QualityIssueStatus,
    QualityResolutionAction,
    QualitySeverity,
    RequestQualityRefetch,
)
from long_invest.modules.market_data.models import DataQualityIssue
from long_invest.platform.errors import AppError
from long_invest.platform.outbox.models import EventOutbox
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
        dedupe_key = f"quality-refetch:{issue.id}:{key_hash}"
        request_hash = _refetch_request_hash(issue, command)
        existing = await self._find_by_dedupe_key(dedupe_key)
        if existing is not None:
            _validate_refetch_replay(existing, request_hash)
            return

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
                "request_hash": request_hash,
            },
            dedupe_key=dedupe_key,
        )
        existing = await self._find_by_dedupe_key(dedupe_key)
        if existing is not None:
            _validate_refetch_replay(existing, request_hash)

    async def _find_by_dedupe_key(self, dedupe_key: str) -> EventOutbox | None:
        return await self.session.scalar(
            select(EventOutbox).where(EventOutbox.dedupe_key == dedupe_key)
        )


def _refetch_request_hash(
    issue: DataQualityIssue,
    command: RequestQualityRefetch,
) -> str:
    canonical = json.dumps(
        {
            "issue_id": str(issue.id),
            "actor_user_id": command.actor_user_id,
            "reason": command.reason,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _validate_refetch_replay(event: EventOutbox, request_hash: str) -> None:
    if event.payload.get("request_hash") != request_hash:
        raise AppError(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message="该幂等键已用于不同的重新抓取请求",
            status_code=409,
        )
