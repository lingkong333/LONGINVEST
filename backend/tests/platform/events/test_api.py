from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.application import get_auth_application
from long_invest.modules.auth.dependencies import (
    AUTH_COOKIE_NAME,
    AuthenticatedRequest,
    require_authenticated_request,
)
from long_invest.platform.events.api import router
from long_invest.platform.events.application import get_event_stream_service


class FakeStreamService:
    def __init__(self) -> None:
        self.last_event_id = None

    async def resolve_cursor(self, last_event_id):
        self.last_event_id = last_event_id
        return 40

    async def stream(self, **_kwargs):
        yield (
            "id: 41\nevent: resource.changed\ndata: "
            '{"resource_type":"jobs","resource_id":"job-41",'
            '"version":41,"topic":"jobs.dispatch"}\n\n'
        )


class FakeAuthService:
    async def authenticate(self, **_kwargs):
        return None


def test_stream_endpoint_is_authenticated_and_sets_sse_proxy_headers() -> None:
    app = FastAPI()
    app.include_router(router)
    stream_service = FakeStreamService()
    authenticated = AuthenticatedRequest(
        user=SimpleNamespace(id="user-1"),
        session=SimpleNamespace(id="session-1"),
        audit_context=SimpleNamespace(trusted_ip="127.0.0.1"),
    )
    app.dependency_overrides[require_authenticated_request] = lambda: authenticated
    app.dependency_overrides[get_event_stream_service] = lambda: stream_service
    app.dependency_overrides[get_auth_application] = FakeAuthService

    client = TestClient(app)
    client.cookies.set(AUTH_COOKIE_NAME, "opaque-token")
    response = client.get(
        "/api/v1/events/stream",
        headers={"Last-Event-ID": "40"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text.startswith("id: 41\nevent: resource.changed")
    assert stream_service.last_event_id == "40"
