from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.providers.api import get_provider_service, router
from long_invest.platform.http.exception_handlers import register_exception_handlers


class FakeService:
    def __init__(self) -> None:
        self.settings_call = None
        self.actions = []

    async def list_providers(self):
        return [{"provider_code": "EASTMONEY"}]

    async def get_provider(self, provider_code):
        return {"provider_code": provider_code.value}

    async def capabilities(self, provider_code):
        return []

    async def health(self, provider_code):
        return []

    async def update_settings(
        self, provider_code, settings, *, expected_version, reason, audit_context
    ):
        self.settings_call = (
            provider_code,
            settings,
            expected_version,
            reason,
            audit_context.idempotency_key,
        )
        return {"version": 2}

    async def list_circuits(self):
        return [{"id": str(uuid4()), "state": "OPEN"}]

    async def probe_circuit(self, circuit_id, *, reason, audit_context):
        self.actions.append(
            ("probe", circuit_id, reason, audit_context.idempotency_key)
        )
        return {"id": str(circuit_id), "state": "CLOSED"}

    async def reset_circuit(self, circuit_id, *, reason, audit_context):
        self.actions.append(
            ("reset", circuit_id, reason, audit_context.idempotency_key)
        )
        return {"id": str(circuit_id), "state": "HALF_OPEN"}

    async def quote_diagnostics(self, symbols, *, reason, audit_context):
        del reason, audit_context
        return {"symbols": symbols, "sources": []}


def app_client(service: FakeService) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    request = AuthenticatedRequest(
        user=object(),
        session=object(),
        audit_context=AuditContext(
            request_id="request-1",
            idempotency_key="request-1",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
        ),
    )
    app.dependency_overrides[get_provider_service] = lambda: service
    app.dependency_overrides[require_authenticated_request] = lambda: request
    app.dependency_overrides[require_verified_write_request] = lambda: request
    return TestClient(app, raise_server_exceptions=False)


def test_api_defines_exact_v31_nine_routes() -> None:
    paths = {
        (method, route.path) for route in router.routes for method in route.methods
    }
    assert paths == {
        ("GET", "/api/v1/providers"),
        ("GET", "/api/v1/providers/{provider_code}"),
        ("GET", "/api/v1/providers/{provider_code}/capabilities"),
        ("GET", "/api/v1/providers/{provider_code}/health"),
        ("PATCH", "/api/v1/providers/{provider_code}/settings"),
        ("GET", "/api/v1/providers/circuits"),
        ("POST", "/api/v1/providers/circuits/{circuit_id}/probe"),
        ("POST", "/api/v1/providers/circuits/{circuit_id}/reset"),
        ("POST", "/api/v1/providers/quote-diagnostics"),
    }


def test_read_api_uses_authenticated_dependency() -> None:
    client = app_client(FakeService())
    providers = client.get("/api/v1/providers")
    circuits = client.get("/api/v1/providers/circuits")

    assert providers.status_code == 200
    assert providers.json()["data"][0] == {
        "provider_code": "EASTMONEY",
        "allowed_actions": ["UPDATE_SETTINGS", "QUOTE_DIAGNOSTICS"],
    }
    assert circuits.json()["data"][0]["allowed_actions"] == ["PROBE", "RESET"]


def test_settings_reject_unsafe_fields_and_require_confirmation_reason() -> None:
    client = app_client(FakeService())
    unsafe = client.patch(
        "/api/v1/providers/EASTMONEY/settings",
        json={"confirm": True, "reason": "test", "url": "https://evil.test"},
        headers={"Idempotency-Key": "idem-unsafe"},
    )
    unconfirmed = client.patch(
        "/api/v1/providers/EASTMONEY/settings",
        json={
            "confirm": False,
            "reason": "test",
            "expected_version": 1,
            "enabled": True,
        },
        headers={"Idempotency-Key": "idem-unconfirmed"},
    )
    assert unsafe.status_code == 422
    assert unconfirmed.status_code == 422


def test_settings_accept_only_safe_fields_and_forward_idempotent_audit_context() -> (
    None
):
    service = FakeService()
    response = app_client(service).patch(
        "/api/v1/providers/EASTMONEY/settings",
        json={
            "confirm": True,
            "reason": "planned adjustment",
            "expected_version": 1,
            "enabled": True,
            "priority": 1,
            "concurrency": 2,
            "rate_per_second": 3,
            "timeout_seconds": 4,
            "auto_switch": True,
        },
        headers={"Idempotency-Key": "explicit-idem"},
    )
    assert response.status_code == 200
    assert service.settings_call[2:] == (
        1,
        "planned adjustment",
        "explicit-idem",
    )


def test_probe_reset_and_diagnostic_write_apis_require_confirmed_safe_input() -> None:
    service = FakeService()
    client = app_client(service)
    circuit_id = uuid4()
    probe = client.post(
        f"/api/v1/providers/circuits/{circuit_id}/probe",
        json={"confirm": True, "reason": "health check"},
        headers={"Idempotency-Key": "probe-idem"},
    )
    reset = client.post(
        f"/api/v1/providers/circuits/{circuit_id}/reset",
        json={"confirm": True, "reason": "operator reset"},
        headers={"Idempotency-Key": "reset-idem"},
    )
    invalid_diagnostic = client.post(
        "/api/v1/providers/quote-diagnostics",
        json={"confirm": True, "reason": "compare", "symbols": ["600000"]},
        headers={"Idempotency-Key": "diagnostic-idem"},
    )
    assert probe.status_code == 200
    assert reset.status_code == 200
    assert invalid_diagnostic.status_code == 422
    assert [item[0] for item in service.actions] == ["probe", "reset"]


def test_all_provider_write_apis_reject_missing_or_blank_idempotency_key() -> None:
    client = app_client(FakeService())
    circuit_id = uuid4()
    requests = [
        ("patch", "/api/v1/providers/EASTMONEY/settings", {
            "confirm": True, "reason": "change", "expected_version": 0,
        }),
        ("post", f"/api/v1/providers/circuits/{circuit_id}/probe", {
            "confirm": True, "reason": "probe",
        }),
        ("post", f"/api/v1/providers/circuits/{circuit_id}/reset", {
            "confirm": True, "reason": "reset",
        }),
        ("post", "/api/v1/providers/quote-diagnostics", {
            "confirm": True, "reason": "compare", "symbols": ["600000.SH"],
        }),
    ]
    for method, path, body in requests:
        missing = getattr(client, method)(path, json=body)
        blank = getattr(client, method)(
            path, json=body, headers={"Idempotency-Key": "   "}
        )
        assert missing.status_code == 422
        assert blank.status_code == 422
