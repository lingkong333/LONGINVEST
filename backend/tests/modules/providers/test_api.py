from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.providers.api import get_provider_service, router
from long_invest.platform.http.exception_handlers import register_exception_handlers


class FakeService:
    def __init__(self) -> None:
        self.settings_call = None

    async def list_providers(self):
        return [{"provider_code": "EASTMONEY"}]

    async def get_provider(self, provider_code):
        return {"provider_code": provider_code.value}

    async def capabilities(self, provider_code):
        return []

    async def health(self, provider_code):
        return []

    async def update_settings(self, provider_code, settings, *, reason, audit_context):
        self.settings_call = (
            provider_code,
            settings,
            reason,
            audit_context.idempotency_key,
        )
        return {"version": 2}

    async def list_circuits(self):
        return []

    async def probe_circuit(self, circuit_id, *, reason, audit_context):
        return {"id": str(circuit_id), "state": "CLOSED"}

    async def reset_circuit(self, circuit_id, *, reason, audit_context):
        return {"id": str(circuit_id), "state": "HALF_OPEN"}

    async def quote_diagnostics(self, symbols):
        return {"symbols": symbols, "sources": []}


def app_client(service: FakeService) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    request = SimpleNamespace(audit_context=SimpleNamespace(idempotency_key="idem-1"))
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
    response = app_client(FakeService()).get("/api/v1/providers")
    assert response.status_code == 200
    assert response.json()["data"][0]["provider_code"] == "EASTMONEY"


def test_settings_reject_unsafe_fields_and_require_confirmation_reason() -> None:
    client = app_client(FakeService())
    unsafe = client.patch(
        "/api/v1/providers/EASTMONEY/settings",
        json={"confirm": True, "reason": "test", "url": "https://evil.test"},
    )
    unconfirmed = client.patch(
        "/api/v1/providers/EASTMONEY/settings",
        json={"confirm": False, "reason": "test", "enabled": True},
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
            "enabled": True,
            "priority": 1,
            "concurrency": 2,
            "rate_per_second": 3,
            "timeout_seconds": 4,
            "auto_switch": True,
        },
    )
    assert response.status_code == 200
    assert service.settings_call[2:] == ("planned adjustment", "idem-1")
