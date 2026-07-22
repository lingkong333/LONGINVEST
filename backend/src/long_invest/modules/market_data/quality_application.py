from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.market_data.contracts import (
    QualityIssuePage,
    QualityIssueStatus,
    QualityIssueView,
    QualityResolutionAction,
    RequestQualityRefetch,
    ResolveQualityIssue,
)
from long_invest.modules.market_data.integrations import (
    TransactionalQualityEventAdapter,
)
from long_invest.modules.market_data.models import DataQualityIssue
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import (
    QualityIssueService,
    ResolveQualityIssueResult,
)
from long_invest.platform.audit.contracts import AuditRecord, AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


@dataclass(frozen=True, slots=True)
class QualityAuditContext:
    request_id: str
    actor_user_id: str
    session_id: str
    trusted_ip: str


class QualityIssueApplication:
    def __init__(
        self,
        database: Database,
        *,
        repository_factory: Callable[[Any], Any] = QualityIssueRepository,
        service_factory: Callable[..., Any] = QualityIssueService,
        event_factory: Callable[[Any], Any] = TransactionalQualityEventAdapter,
        audit_factory: Callable[[Any], Any] = AuditService,
    ) -> None:
        self._database = database
        self._repository_factory = repository_factory
        self._service_factory = service_factory
        self._event_factory = event_factory
        self._audit_factory = audit_factory

    def _components(self, session: Any) -> tuple[Any, Any, Any]:
        repository = self._repository_factory(session)
        service = self._service_factory(
            repository,
            events=self._event_factory(session),
        )
        return repository, service, self._audit_factory(session)

    async def list(
        self,
        *,
        status: QualityIssueStatus | None = None,
        issue_type: str | None = None,
        symbol: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> QualityIssuePage:
        try:
            async with self._database.session() as session:
                _, service, _ = self._components(session)
                return await service.list(
                    status=status,
                    issue_type=issue_type,
                    symbol=symbol,
                    page=page,
                    page_size=page_size,
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def get(self, issue_id: UUID) -> QualityIssueView:
        try:
            async with self._database.transaction() as session:
                repository, _, _ = self._components(session)
                return _view(await _require_issue(repository, issue_id))
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def select_source(
        self,
        issue_id: UUID,
        *,
        selected_source: str,
        reason: str,
        idempotency_key: str,
        audit_context: QualityAuditContext,
    ) -> ResolveQualityIssueResult:
        return await self._resolve(
            issue_id,
            action=QualityResolutionAction.SELECT_SOURCE,
            selected_source=selected_source,
            reason=reason,
            idempotency_key=idempotency_key,
            audit_context=audit_context,
        )

    async def invalidate(
        self,
        issue_id: UUID,
        *,
        reason: str,
        idempotency_key: str,
        audit_context: QualityAuditContext,
    ) -> ResolveQualityIssueResult:
        return await self._resolve(
            issue_id,
            action=QualityResolutionAction.INVALIDATE,
            selected_source=None,
            reason=reason,
            idempotency_key=idempotency_key,
            audit_context=audit_context,
        )

    async def request_refetch(
        self,
        issue_id: UUID,
        *,
        reason: str,
        idempotency_key: str,
        audit_context: QualityAuditContext,
    ) -> QualityIssueView:
        action_code = "DATA_QUALITY_REFETCH_REQUEST"
        expected_after = {"action": QualityResolutionAction.REFETCH.value}
        try:
            async with self._database.transaction() as session:
                repository, service, audit = self._components(session)
                replay = await audit.find_by_idempotency(idempotency_key)
                if replay is not None:
                    _validate_replay(
                        replay,
                        action_code=action_code,
                        issue_id=issue_id,
                        reason=reason,
                        after_summary=expected_after,
                    )
                    return _view(await _require_issue(repository, issue_id))

                issue = await service.request_refetch(
                    RequestQualityRefetch(
                        issue_id=issue_id,
                        actor_user_id=audit_context.actor_user_id,
                        reason=reason,
                        idempotency_key=idempotency_key,
                    )
                )
                await _append_audit(
                    audit,
                    action_code=action_code,
                    issue_id=issue_id,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    context=audit_context,
                    before_summary={"status": issue.status.value},
                    after_summary=expected_after,
                )
                return issue
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def _resolve(
        self,
        issue_id: UUID,
        *,
        action: QualityResolutionAction,
        selected_source: str | None,
        reason: str,
        idempotency_key: str,
        audit_context: QualityAuditContext,
    ) -> ResolveQualityIssueResult:
        action_code = f"DATA_QUALITY_{action.value}"
        expected_after = {
            "action": action.value,
            "selected_source": selected_source,
        }
        try:
            async with self._database.transaction() as session:
                repository, service, audit = self._components(session)
                replay = await audit.find_by_idempotency(idempotency_key)
                if replay is not None:
                    _validate_replay(
                        replay,
                        action_code=action_code,
                        issue_id=issue_id,
                        reason=reason,
                        after_summary=expected_after,
                    )
                    return ResolveQualityIssueResult(
                        issue=_view(await _require_issue(repository, issue_id)),
                        replayed=True,
                    )

                result = await service.resolve(
                    ResolveQualityIssue(
                        issue_id=issue_id,
                        action=action,
                        actor_user_id=audit_context.actor_user_id,
                        reason=reason,
                        selected_source=selected_source,
                    )
                )
                await _append_audit(
                    audit,
                    action_code=action_code,
                    issue_id=issue_id,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    context=audit_context,
                    before_summary=None,
                    after_summary=expected_after,
                    result="REPLAYED" if result.replayed else "SUCCEEDED",
                )
                return result
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


async def _require_issue(repository: Any, issue_id: UUID) -> DataQualityIssue:
    issue = await repository.get_for_update(issue_id)
    if issue is None:
        raise AppError(
            code="QUALITY_ISSUE_NOT_FOUND",
            message="数据质量问题不存在",
            status_code=404,
        )
    return issue


def _view(issue: DataQualityIssue) -> QualityIssueView:
    return QualityIssueView(
        id=issue.id,
        issue_type=issue.issue_type,
        subject_type=issue.subject_type,
        subject_id=issue.subject_id,
        symbol=issue.symbol,
        status=issue.status,
        severity=issue.severity,
        evidence=issue.evidence,
        occurrence_count=issue.occurrence_count,
        first_seen_at=issue.first_seen_at,
        last_seen_at=issue.last_seen_at,
        resolved_at=issue.resolved_at,
        resolved_by_user_id=issue.resolved_by_user_id,
        resolution_action=issue.resolution_action,
        resolution_reason=issue.resolution_reason,
        selected_source=issue.selected_source,
    )


async def _append_audit(
    audit: Any,
    *,
    action_code: str,
    issue_id: UUID,
    reason: str,
    idempotency_key: str,
    context: QualityAuditContext,
    before_summary: dict[str, Any] | None,
    after_summary: dict[str, Any],
    result: str = "SUCCEEDED",
) -> None:
    await audit.append(
        AuditWrite(
            action_code=action_code,
            object_type="data_quality_issue",
            object_id=str(issue_id),
            result=result,
            request_id=context.request_id,
            idempotency_key=idempotency_key,
            risk_level="HIGH",
            reason=reason,
            before_summary=before_summary,
            after_summary=after_summary,
            actor_user_id=context.actor_user_id,
            session_id=context.session_id,
            trusted_ip=context.trusted_ip,
        )
    )


def _validate_replay(
    record: AuditRecord,
    *,
    action_code: str,
    issue_id: UUID,
    reason: str,
    after_summary: dict[str, Any],
) -> None:
    if (
        record.action_code != action_code
        or record.object_type != "data_quality_issue"
        or record.object_id != str(issue_id)
        or record.reason != reason
        or record.after_summary != after_summary
    ):
        raise AppError(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message="该幂等键已用于不同的数据质量操作",
            status_code=409,
        )


def get_quality_issue_application() -> QualityIssueApplication:
    return QualityIssueApplication(get_database())


def _backend_unavailable() -> AppError:
    return AppError(
        code="DATA_QUALITY_BACKEND_UNAVAILABLE",
        message="数据质量服务暂时不可用",
        status_code=503,
    )
