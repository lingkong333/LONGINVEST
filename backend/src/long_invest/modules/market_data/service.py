from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from long_invest.modules.market_data.contracts import (
    OpenQualityIssue,
    QualityIssueStatus,
    QualityResolutionAction,
    QualitySeverity,
    ResolveQualityIssue,
)
from long_invest.modules.market_data.models import DataQualityIssue
from long_invest.platform.errors import AppError

TERMINAL_STATUSES = frozenset(
    {QualityIssueStatus.RESOLVED, QualityIssueStatus.INVALIDATED}
)
SEVERITY_RANK = {
    QualitySeverity.INFO: 0,
    QualitySeverity.WARNING: 1,
    QualitySeverity.ERROR: 2,
    QualitySeverity.CRITICAL: 3,
}


class QualityIssueRepositoryPort(Protocol):
    async def find_by_dedupe_key(self, dedupe_key: str) -> DataQualityIssue | None: ...

    async def get_for_update(self, issue_id: UUID) -> DataQualityIssue | None: ...

    async def claim_issue(
        self, record: DataQualityIssue
    ) -> tuple[DataQualityIssue, bool]: ...

    async def flush(self) -> None: ...


@dataclass(frozen=True, slots=True)
class OpenQualityIssueResult:
    issue: DataQualityIssue
    created: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class ResolveQualityIssueResult:
    issue: DataQualityIssue
    replayed: bool


class QualityIssueService:
    def __init__(
        self,
        repository: QualityIssueRepositoryPort,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._now = now_provider or (lambda: datetime.now(UTC))

    async def open(self, command: OpenQualityIssue) -> OpenQualityIssueResult:
        existing = await self._repository.find_by_dedupe_key(command.dedupe_key)
        if existing is None:
            now = self._now()
            candidate = DataQualityIssue(
                issue_type=command.issue_type,
                subject_type=command.subject_type,
                subject_id=command.subject_id,
                symbol=command.symbol,
                status=(
                    QualityIssueStatus.REVIEW_REQUIRED
                    if command.requires_review
                    else QualityIssueStatus.OPEN
                ),
                severity=command.severity,
                evidence=_json_copy(command.evidence),
                dedupe_key=command.dedupe_key,
                occurrence_count=1,
                first_seen_at=now,
                last_seen_at=now,
            )
            claimed, created = await self._repository.claim_issue(candidate)
            if created:
                return OpenQualityIssueResult(
                    issue=claimed,
                    created=True,
                    replayed=False,
                )
            existing = claimed

        locked = await self._repository.get_for_update(existing.id)
        if locked is None:
            raise AppError(
                code="QUALITY_ISSUE_NOT_FOUND",
                message="数据质量问题不存在",
                status_code=404,
            )
        status = _issue_status(locked)
        if status in TERMINAL_STATUSES:
            return OpenQualityIssueResult(
                issue=locked,
                created=False,
                replayed=True,
            )

        current_severity = _severity(locked)
        if SEVERITY_RANK[command.severity] > SEVERITY_RANK[current_severity]:
            locked.severity = command.severity
        locked.occurrence_count += 1
        locked.last_seen_at = self._now()
        locked.evidence = _json_copy(command.evidence)
        await self._repository.flush()
        return OpenQualityIssueResult(
            issue=locked,
            created=False,
            replayed=True,
        )

    async def resolve(
        self,
        command: ResolveQualityIssue,
    ) -> ResolveQualityIssueResult:
        issue = await self._repository.get_for_update(command.issue_id)
        if issue is None:
            raise AppError(
                code="QUALITY_ISSUE_NOT_FOUND",
                message="数据质量问题不存在",
                status_code=404,
            )
        action = _resolution_action(command.action)
        status = _issue_status(issue)
        if status in TERMINAL_STATUSES:
            if _same_resolution(issue, command, action):
                return ResolveQualityIssueResult(issue=issue, replayed=True)
            raise AppError(
                code="QUALITY_ISSUE_STATE_CONFLICT",
                message="数据质量问题已按其他方式处理",
                status_code=409,
            )
        if status not in {
            QualityIssueStatus.OPEN,
            QualityIssueStatus.REVIEW_REQUIRED,
        }:
            raise AppError(
                code="QUALITY_ISSUE_STATE_INVALID",
                message="数据质量问题状态不允许处理",
                status_code=409,
            )
        if action is QualityResolutionAction.REFETCH:
            raise AppError(
                code="QUALITY_ACTION_NOT_ALLOWED",
                message="重新抓取不属于终态裁决",
                status_code=422,
            )
        if action is QualityResolutionAction.SELECT_SOURCE:
            _validate_selected_source(issue, command.selected_source)

        issue.status = (
            QualityIssueStatus.INVALIDATED
            if action is QualityResolutionAction.INVALIDATE
            else QualityIssueStatus.RESOLVED
        )
        issue.resolved_at = self._now()
        issue.resolved_by_user_id = command.actor_user_id
        issue.resolution_action = action
        issue.resolution_reason = command.reason
        issue.selected_source = command.selected_source
        await self._repository.flush()
        return ResolveQualityIssueResult(issue=issue, replayed=False)


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(value, allow_nan=False))


def _issue_status(issue: DataQualityIssue) -> QualityIssueStatus:
    try:
        return QualityIssueStatus(issue.status)
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="QUALITY_ISSUE_STATE_INVALID",
            message="数据质量问题状态无效",
            status_code=409,
        ) from exc


def _severity(issue: DataQualityIssue) -> QualitySeverity:
    try:
        return QualitySeverity(issue.severity)
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="QUALITY_SEVERITY_INVALID",
            message="数据质量问题严重度无效",
            status_code=422,
        ) from exc


def _resolution_action(value: object) -> QualityResolutionAction:
    try:
        return QualityResolutionAction(value)
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="QUALITY_ACTION_NOT_ALLOWED",
            message="不支持的数据质量处理动作",
            status_code=422,
        ) from exc


def _same_resolution(
    issue: DataQualityIssue,
    command: ResolveQualityIssue,
    action: QualityResolutionAction,
) -> bool:
    return (
        issue.resolution_action == action
        and issue.resolved_by_user_id == command.actor_user_id
        and issue.resolution_reason == command.reason
        and issue.selected_source == command.selected_source
    )


def _validate_selected_source(
    issue: DataQualityIssue,
    selected_source: str | None,
) -> None:
    if not isinstance(issue.evidence, Mapping):
        raise AppError(
            code="QUALITY_EVIDENCE_INVALID",
            message="数据质量证据必须是对象",
            status_code=422,
        )
    sources = issue.evidence.get("sources")
    if not isinstance(sources, Mapping):
        raise AppError(
            code="QUALITY_EVIDENCE_INVALID",
            message="数据质量证据中的来源必须是对象",
            status_code=422,
        )
    if selected_source not in sources:
        raise AppError(
            code="QUALITY_SOURCE_NOT_AVAILABLE",
            message="选择的来源不在已保存证据中",
            status_code=422,
        )
