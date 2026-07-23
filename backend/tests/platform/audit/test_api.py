from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.dependencies import require_authenticated_request
from long_invest.platform.audit.api import (
    get_audit_query_application,
    router,
)
from long_invest.platform.audit.query import AuditEventPage, AuditEventView
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import register_exception_handlers

NOW = datetime(2026, 7, 22, 10, tzinfo=UTC)


def _event() -> AuditEventView:
    return AuditEventView(
        id=uuid4(),
        occurred_at=NOW,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        action_code="TARGET_UPDATE",
        object_type="target",
        object_id="target-1",
        result="SUCCESS",
        before_summary={"safe": "before"},
        after_summary={"safe": "after"},
        reason="manual update",
        request_id="req-1",
        idempotency_key="idem-1",
        risk_level="HIGH",
    )


def _application():
    return SimpleNamespace(
        list_events=AsyncMock(
            return_value=AuditEventPage(
                items=(_event(),), total=21, page=2, page_size=10
            )
        )
    )


def _app(application=None, *, authenticated=True) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_audit_query_application] = lambda: (
        application or _application()
    )
    if authenticated:
        app.dependency_overrides[require_authenticated_request] = lambda: (
            SimpleNamespace(user=SimpleNamespace(id=uuid4()))
        )
    else:

        async def reject():
            raise AppError(code="AUTH_REQUIRED", message="login", status_code=401)

        app.dependency_overrides[require_authenticated_request] = reject
    return app


def test_audit_route_requires_login_and_has_concrete_response_contract() -> None:
    route = router.routes[0]
    dependencies = {item.call for item in route.dependant.dependencies}

    assert route.path == "/api/v1/audit-events"
    assert route.methods == {"GET"}
    assert require_authenticated_request in dependencies
    assert route.response_model not in {None, dict}
    assert TestClient(_app(authenticated=False)).get(route.path).status_code == 401


def test_audit_api_forwards_filters_and_returns_safe_explicit_fields() -> None:
    application = _application()
    client = TestClient(_app(application))
    response = client.get(
        "/api/v1/audit-events",
        params={
            "page": 2,
            "page_size": 10,
            "start_at": "2026-07-01T00:00:00Z",
            "end_at": "2026-07-22T10:00:00Z",
            "actor_user_id": "user-1",
            "action_code": "TARGET_UPDATE",
            "object_type": "target",
            "object_id": "target-1",
            "result": "SUCCESS",
            "risk_level": "HIGH",
            "request_id": "req-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "items": [
            {
                "id": str(application.list_events.return_value.items[0].id),
                "occurred_at": "2026-07-22T10:00:00Z",
                "actor_user_id": "user-1",
                "session_id": "session-1",
                "trusted_ip": "127.0.0.1",
                "action_code": "TARGET_UPDATE",
                "object_type": "target",
                "object_id": "target-1",
                "result": "SUCCESS",
                "before_summary": {"safe": "before"},
                "after_summary": {"safe": "after"},
                "reason": "manual update",
                "request_id": "req-1",
                "idempotency_key": "idem-1",
                "risk_level": "HIGH",
            }
        ],
        "pagination": {"page": 2, "page_size": 10, "total": 21},
        "allowed_actions": [],
    }
    application.list_events.assert_awaited_once_with(
        page=2,
        page_size=10,
        start_at=datetime(2026, 7, 1, tzinfo=UTC),
        end_at=NOW,
        actor_user_id="user-1",
        action_code="TARGET_UPDATE",
        object_type="target",
        object_id="target-1",
        result="SUCCESS",
        risk_level="HIGH",
        request_id="req-1",
    )


def test_audit_api_rejects_invalid_pagination_and_oversized_filters() -> None:
    client = TestClient(_app())

    invalid_page = client.get("/api/v1/audit-events?page=0&page_size=201")
    oversized = client.get("/api/v1/audit-events", params={"request_id": "r" * 65})

    assert invalid_page.status_code == 422
    assert oversized.status_code == 422


def test_audit_openapi_exposes_one_unique_typed_operation() -> None:
    operation = _app().openapi()["paths"]["/api/v1/audit-events"]["get"]

    assert operation["operationId"] == "list_audit_events_api_v1_audit_events_get"
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("AuditEventPageEnvelope")
    page_schema = _app().openapi()["components"]["schemas"]["AuditEventPageResponse"]
    assert "allowed_actions" in page_schema["required"]
