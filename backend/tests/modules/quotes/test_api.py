from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.quotes.api import get_quote_application, router
from long_invest.platform.http.exception_handlers import register_exception_handlers


class FakeApplication:
    def __init__(self) -> None:
        self.calls = []
        self.job = SimpleNamespace(id=uuid4(), status="PENDING_DISPATCH")

    async def list_cycles(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"items": [], "total": 0, **kwargs}

    async def list_items(self, cycle_id, **kwargs):
        self.calls.append(("items", cycle_id, kwargs))
        return []

    async def submit_manual(self, **kwargs):
        self.calls.append(("manual", kwargs))
        return self.job

    async def submit_diagnostic(self, **kwargs):
        self.calls.append(("diagnostic", kwargs))
        return self.job


def client(application=None, *, auth=True, write=True):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    application = application or FakeApplication()
    app.dependency_overrides[get_quote_application] = lambda: application
    identity = AuthenticatedRequest(
        user=SimpleNamespace(id="user-1"),
        session=SimpleNamespace(id="session-1"),
        audit_context=AuditContext(
            request_id="request-1",
            idempotency_key="request-1",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
        ),
    )
    if auth:
        app.dependency_overrides[require_authenticated_request] = lambda: identity
    if write:
        app.dependency_overrides[require_verified_write_request] = lambda: identity
    return TestClient(app, raise_server_exceptions=False), application


def test_router_exposes_exactly_four_v31_routes() -> None:
    assert {
        (method, route.path) for route in router.routes for method in route.methods
    } == {
        ("GET", "/api/v1/quote-cycles"),
        ("GET", "/api/v1/quote-cycles/{cycle_id}/items"),
        ("POST", "/api/v1/quote-cycles/manual"),
        ("POST", "/api/v1/quotes/diagnose"),
    }


def test_read_and_write_routes_use_published_auth_dependencies() -> None:
    for route in router.routes:
        calls = {dependency.call for dependency in route.dependant.dependencies}
        expected = (
            require_authenticated_request
            if "GET" in route.methods
            else require_verified_write_request
        )
        assert expected in calls


def test_manual_requires_confirmation_and_idempotency_key() -> None:
    http, application = client()
    body = {"symbols": ["600000.SH"], "confirm": False, "timeout_seconds": 30}
    unconfirmed = http.post(
        "/api/v1/quote-cycles/manual",
        json=body,
        headers={"Idempotency-Key": "manual-1"},
    )
    body["confirm"] = True
    missing_key = http.post("/api/v1/quote-cycles/manual", json=body)
    blank_key = http.post(
        "/api/v1/quote-cycles/manual",
        json=body,
        headers={"Idempotency-Key": "   "},
    )
    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["code"] == "AUTH_CONFIRMATION_REQUIRED"
    assert missing_key.status_code == 422
    assert blank_key.status_code == 422
    assert application.calls == []


def test_manual_submits_job_without_creating_cycle_or_waiting_for_provider() -> None:
    http, application = client()
    response = http.post(
        "/api/v1/quote-cycles/manual",
        json={"symbols": ["600000.SH"], "confirm": True, "timeout_seconds": 45},
        headers={"Idempotency-Key": "manual-1"},
    )
    assert response.status_code == 202
    assert response.json()["data"]["job_id"] == str(application.job.id)
    call = application.calls[0]
    assert call[0] == "manual"
    assert call[1]["symbols"] == ("600000.SH",)
    assert call[1]["idempotency_key"] == "manual-1"


def test_diagnostic_submits_separate_job_and_does_not_call_manual() -> None:
    http, application = client()
    response = http.post(
        "/api/v1/quotes/diagnose",
        json={"symbols": ["600000.SH"], "confirm": True},
        headers={"Idempotency-Key": "diagnose-1"},
    )
    assert response.status_code == 202
    assert [call[0] for call in application.calls] == ["diagnostic"]


def test_cycle_and_item_queries_forward_stable_pagination() -> None:
    http, application = client()
    cycle_id = uuid4()
    assert http.get("/api/v1/quote-cycles?page=2&page_size=10").status_code == 200
    assert (
        http.get(
            f"/api/v1/quote-cycles/{cycle_id}/items?page=1&page_size=20"
        ).status_code
        == 200
    )
    assert application.calls == [
        ("list", {"status": None, "page": 2, "page_size": 10}),
        ("items", cycle_id, {"page": 1, "page_size": 20}),
    ]
