from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.market_data.contracts import (
    QualityIssuePage,
    QualityIssueStatus,
    QualityIssueView,
    QualitySeverity,
)
from long_invest.modules.market_data.quality_api import router
from long_invest.modules.market_data.quality_application import (
    get_quality_issue_application,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import register_exception_handlers

NOW = datetime(2026, 7, 22, 8, tzinfo=UTC)


def issue_view() -> QualityIssueView:
    return QualityIssueView(
        id=uuid4(),
        issue_type="QUOTE_CONFLICT",
        subject_type="quote_cycle_item",
        subject_id="item-1",
        symbol="600000.SH",
        status=QualityIssueStatus.REVIEW_REQUIRED,
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


def client_for(application):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    identity = AuthenticatedRequest(
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=uuid4()),
        audit_context=AuditContext(
            request_id="request-quality",
            idempotency_key="request-quality",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
        ),
    )
    app.dependency_overrides[get_quality_issue_application] = lambda: application
    app.dependency_overrides[require_authenticated_request] = lambda: identity
    app.dependency_overrides[require_verified_write_request] = lambda: identity
    return TestClient(app, raise_server_exceptions=False), identity, app


def fake_application(view: QualityIssueView):
    application = Mock()
    application.list = AsyncMock(
        return_value=QualityIssuePage(items=(), total=0, page=1, page_size=50)
    )
    application.get = AsyncMock(return_value=view)
    application.select_source = AsyncMock(
        return_value=SimpleNamespace(issue=view, replayed=False)
    )
    application.invalidate = AsyncMock(
        return_value=SimpleNamespace(issue=view, replayed=False)
    )
    application.request_refetch = AsyncMock(return_value=view)
    return application


def test_detail_exposes_saved_source_candidates_and_allowed_actions() -> None:
    application = fake_application(issue_view())
    client, _, app = client_for(application)
    try:
        response = client.get(f"/api/v1/data-quality/issues/{issue_view().id}")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["source_candidates"] == ["EASTMONEY", "SINA"]
        assert data["allowed_actions"] == [
            "SELECT_SOURCE",
            "INVALIDATE",
            "REFETCH",
        ]
    finally:
        app.dependency_overrides.clear()


def test_routes_cover_list_detail_and_three_safe_actions() -> None:
    paths = {
        (method, route.path) for route in router.routes for method in route.methods
    }
    assert paths == {
        ("GET", "/api/v1/data-quality/issues"),
        ("GET", "/api/v1/data-quality/issues/{issue_id}"),
        ("POST", "/api/v1/data-quality/issues/{issue_id}/select-source"),
        ("POST", "/api/v1/data-quality/issues/{issue_id}/resolve"),
        ("POST", "/api/v1/data-quality/issues/{issue_id}/refetch"),
    }


def test_list_supports_filters_pagination_and_empty_data() -> None:
    view = issue_view()
    application = fake_application(view)
    client, _, _ = client_for(application)

    response = client.get(
        "/api/v1/data-quality/issues",
        params={
            "status": "OPEN",
            "issue_type": "QUOTE_CONFLICT",
            "symbol": "600000.SH",
            "page": 2,
            "page_size": 20,
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "items": [],
        "pagination": {"page": 1, "page_size": 50, "total": 0},
    }
    application.list.assert_awaited_once_with(
        status=QualityIssueStatus.OPEN,
        issue_type="QUOTE_CONFLICT",
        symbol="600000.SH",
        page=2,
        page_size=20,
    )


def test_detail_returns_saved_source_evidence() -> None:
    view = issue_view()
    client, _, _ = client_for(fake_application(view))

    response = client.get(f"/api/v1/data-quality/issues/{view.id}")

    assert response.status_code == 200
    assert set(response.json()["data"]["evidence"]["sources"]) == {
        "EASTMONEY",
        "SINA",
    }


def test_write_rejects_missing_key_confirmation_and_arbitrary_price() -> None:
    view = issue_view()
    application = fake_application(view)
    client, _, _ = client_for(application)
    url = f"/api/v1/data-quality/issues/{view.id}/select-source"

    missing_key = client.post(
        url,
        json={
            "confirm": True,
            "reason": "reviewed",
            "selected_source": "EASTMONEY",
        },
    )
    unconfirmed = client.post(
        url,
        json={
            "confirm": False,
            "reason": "reviewed",
            "selected_source": "EASTMONEY",
        },
        headers={"Idempotency-Key": "quality-1"},
    )
    arbitrary_price = client.post(
        url,
        json={
            "confirm": True,
            "reason": "reviewed",
            "selected_source": "EASTMONEY",
            "price": "99.99",
        },
        headers={"Idempotency-Key": "quality-2"},
    )

    assert missing_key.status_code == 422
    assert unconfirmed.json()["code"] == "AUTH_CONFIRMATION_REQUIRED"
    assert arbitrary_price.status_code == 422
    application.select_source.assert_not_awaited()


def test_select_source_forwards_identity_reason_and_idempotency() -> None:
    view = issue_view()
    application = fake_application(view)
    client, identity, _ = client_for(application)

    response = client.post(
        f"/api/v1/data-quality/issues/{view.id}/select-source",
        json={
            "confirm": True,
            "reason": " evidence reviewed ",
            "selected_source": " EASTMONEY ",
        },
        headers={"Idempotency-Key": "quality-1"},
    )

    assert response.status_code == 200
    arguments = application.select_source.await_args.kwargs
    assert arguments["selected_source"] == "EASTMONEY"
    assert arguments["reason"] == "evidence reviewed"
    assert arguments["idempotency_key"] == "quality-1"
    assert arguments["audit_context"].actor_user_id == str(identity.user.id)


def test_refetch_is_accepted_without_executing_external_work_in_http() -> None:
    view = issue_view()
    application = fake_application(view)
    client, _, _ = client_for(application)

    response = client.post(
        f"/api/v1/data-quality/issues/{view.id}/refetch",
        json={"confirm": True, "reason": "provider recovered"},
        headers={"Idempotency-Key": "refetch-1"},
    )

    assert response.status_code == 202
    assert response.json()["code"] == "REFETCH_ACCEPTED"
    application.request_refetch.assert_awaited_once()


def test_illegal_source_and_terminal_conflict_remain_distinct_errors() -> None:
    view = issue_view()
    application = fake_application(view)
    client, _, _ = client_for(application)
    url = f"/api/v1/data-quality/issues/{view.id}/select-source"
    body = {"confirm": True, "reason": "reviewed", "selected_source": "UNKNOWN"}

    application.select_source.side_effect = AppError(
        code="QUALITY_SOURCE_NOT_AVAILABLE",
        message="source unavailable",
        status_code=422,
    )
    invalid = client.post(url, json=body, headers={"Idempotency-Key": "invalid"})
    application.select_source.side_effect = AppError(
        code="QUALITY_ISSUE_STATE_CONFLICT",
        message="terminal",
        status_code=409,
    )
    terminal = client.post(url, json=body, headers={"Idempotency-Key": "terminal"})

    assert invalid.status_code == 422
    assert invalid.json()["code"] == "QUALITY_SOURCE_NOT_AVAILABLE"
    assert terminal.status_code == 409
    assert terminal.json()["code"] == "QUALITY_ISSUE_STATE_CONFLICT"


def test_openapi_requires_idempotency_header_and_has_concrete_models() -> None:
    view = issue_view()
    _, _, app = client_for(fake_application(view))
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/data-quality/issues/{issue_id}/select-source"][
        "post"
    ]
    header = next(item for item in operation["parameters"] if item["in"] == "header")

    assert header["name"] == "Idempotency-Key"
    assert header["required"] is True
    response_schema = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert response_schema["$ref"] == "#/components/schemas/QualityIssueResponse"
