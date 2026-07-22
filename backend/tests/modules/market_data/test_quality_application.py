from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from long_invest.modules.market_data.contracts import (
    QualityIssuePage,
    QualityIssueStatus,
    QualityIssueView,
    QualitySeverity,
)
from long_invest.modules.market_data.quality_application import (
    QualityAuditContext,
    QualityIssueApplication,
)
from long_invest.modules.market_data.service import ResolveQualityIssueResult
from long_invest.platform.audit.contracts import AuditRecord
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 22, 8, tzinfo=UTC)


def issue_view(*, status=QualityIssueStatus.REVIEW_REQUIRED) -> QualityIssueView:
    return QualityIssueView(
        id=uuid4(),
        issue_type="QUOTE_CONFLICT",
        subject_type="quote_cycle_item",
        subject_id="item-1",
        symbol="600000.SH",
        status=status,
        severity=QualitySeverity.WARNING,
        evidence={
            "sources": {
                "EASTMONEY": {"price": "10.00"},
                "SINA": {"price": "10.10"},
            }
        },
        occurrence_count=1,
        first_seen_at=NOW,
        last_seen_at=NOW,
        resolved_at=None,
        resolved_by_user_id=None,
        resolution_action=None,
        resolution_reason=None,
        selected_source=None,
    )


class FakeDatabase:
    def __init__(self) -> None:
        self.session_value = object()
        self.transactions = 0

    @asynccontextmanager
    async def session(self):
        yield self.session_value

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield self.session_value


class FakeAudit:
    def __init__(self) -> None:
        self.records: dict[str, AuditRecord] = {}
        self.writes = []

    async def find_by_idempotency(self, key):
        return self.records.get(key)

    async def append(self, write):
        self.writes.append(write)
        record = AuditRecord(
            action_code=write.action_code,
            object_type=write.object_type,
            object_id=write.object_id,
            result=write.result,
            request_id=write.request_id,
            idempotency_key=write.idempotency_key,
            risk_level=write.risk_level,
            reason=write.reason,
            before_summary=write.before_summary,
            after_summary=write.after_summary,
            actor_user_id=write.actor_user_id,
            session_id=write.session_id,
            trusted_ip=write.trusted_ip,
        )
        self.records[write.idempotency_key] = record
        return record


def context() -> QualityAuditContext:
    return QualityAuditContext(
        request_id="request-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )


def application_for(view: QualityIssueView):
    database = FakeDatabase()
    stored = SimpleNamespace(**view.__dict__) if hasattr(view, "__dict__") else None
    if stored is None:
        stored = SimpleNamespace(
            **{name: getattr(view, name) for name in view.__dataclass_fields__}
        )
    repository = Mock()
    repository.get_for_update = AsyncMock(return_value=stored)
    service = Mock()
    service.list = AsyncMock(
        return_value=QualityIssuePage(items=(), total=0, page=1, page_size=50)
    )
    service.resolve = AsyncMock(
        return_value=ResolveQualityIssueResult(issue=view, replayed=False)
    )
    service.request_refetch = AsyncMock(return_value=view)
    audit = FakeAudit()
    application = QualityIssueApplication(
        database,
        repository_factory=lambda _session: repository,
        service_factory=lambda _repository, **_kwargs: service,
        event_factory=lambda _session: object(),
        audit_factory=lambda _session: audit,
    )
    return application, database, repository, service, audit


@pytest.mark.anyio
async def test_list_returns_empty_page_and_forwards_filters() -> None:
    view = issue_view()
    application, _, _, service, _ = application_for(view)

    result = await application.list(
        status=QualityIssueStatus.OPEN,
        issue_type="QUOTE_CONFLICT",
        symbol="600000.SH",
        page=2,
        page_size=20,
    )

    assert result.items == ()
    service.list.assert_awaited_once_with(
        status=QualityIssueStatus.OPEN,
        issue_type="QUOTE_CONFLICT",
        symbol="600000.SH",
        page=2,
        page_size=20,
    )


@pytest.mark.anyio
async def test_select_source_audits_in_transaction_and_replays_same_key() -> None:
    view = issue_view()
    application, database, _, service, audit = application_for(view)
    arguments = {
        "selected_source": "EASTMONEY",
        "reason": "evidence reviewed",
        "idempotency_key": "quality-1",
        "audit_context": context(),
    }

    first = await application.select_source(view.id, **arguments)
    replay = await application.select_source(view.id, **arguments)

    assert first.replayed is False
    assert replay.replayed is True
    assert database.transactions == 2
    assert service.resolve.await_count == 1
    assert len(audit.writes) == 1
    assert audit.writes[0].action_code == "DATA_QUALITY_SELECT_SOURCE"
    assert audit.writes[0].after_summary == {
        "action": "SELECT_SOURCE",
        "selected_source": "EASTMONEY",
    }


@pytest.mark.anyio
async def test_same_idempotency_key_rejects_different_source() -> None:
    view = issue_view()
    application, _, _, _, _ = application_for(view)
    common = {
        "reason": "evidence reviewed",
        "idempotency_key": "quality-1",
        "audit_context": context(),
    }
    await application.select_source(view.id, selected_source="EASTMONEY", **common)

    with pytest.raises(AppError) as raised:
        await application.select_source(view.id, selected_source="SINA", **common)

    assert raised.value.code == "IDEMPOTENCY_KEY_CONFLICT"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error_code",
    ["QUALITY_SOURCE_NOT_AVAILABLE", "QUALITY_ISSUE_STATE_CONFLICT"],
)
async def test_business_rejection_does_not_write_audit(error_code: str) -> None:
    view = issue_view()
    application, _, _, service, audit = application_for(view)
    service.resolve.side_effect = AppError(
        code=error_code,
        message="rejected",
        status_code=422 if error_code.endswith("AVAILABLE") else 409,
    )

    with pytest.raises(AppError) as raised:
        await application.select_source(
            view.id,
            selected_source="UNKNOWN",
            reason="review",
            idempotency_key="quality-rejected",
            audit_context=context(),
        )

    assert raised.value.code == error_code
    assert audit.writes == []


@pytest.mark.anyio
async def test_refetch_only_delegates_to_event_backed_service_and_replays() -> None:
    view = issue_view()
    application, _, _, service, audit = application_for(view)
    arguments = {
        "reason": "provider recovered",
        "idempotency_key": "refetch-1",
        "audit_context": context(),
    }

    first = await application.request_refetch(view.id, **arguments)
    replay = await application.request_refetch(view.id, **arguments)

    assert first.id == view.id
    assert replay.id == view.id
    assert service.request_refetch.await_count == 1
    assert len(audit.writes) == 1
    command = service.request_refetch.await_args.args[0]
    assert command.issue_id == view.id
    assert command.idempotency_key == "refetch-1"


@pytest.mark.anyio
async def test_backend_timeout_is_isolated_as_stable_unavailable_error() -> None:
    view = issue_view()
    application, _, _, service, audit = application_for(view)
    service.resolve.side_effect = TimeoutError("database timeout")

    with pytest.raises(AppError) as raised:
        await application.invalidate(
            view.id,
            reason="invalid evidence",
            idempotency_key="quality-timeout",
            audit_context=context(),
        )

    assert raised.value.code == "DATA_QUALITY_BACKEND_UNAVAILABLE"
    assert audit.writes == []


@pytest.mark.anyio
async def test_audit_timeout_fails_the_same_transaction() -> None:
    view = issue_view()
    application, _, _, _, audit = application_for(view)
    audit.append = AsyncMock(side_effect=TimeoutError("audit timeout"))

    with pytest.raises(AppError) as raised:
        await application.invalidate(
            view.id,
            reason="invalid evidence",
            idempotency_key="quality-audit-timeout",
            audit_context=context(),
        )

    assert raised.value.code == "DATA_QUALITY_BACKEND_UNAVAILABLE"
