from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient

from long_invest.bootstrap.app import create_app
from long_invest.modules.auth.application import get_auth_application
from long_invest.modules.auth.dependencies import (
    AUTH_COOKIE_NAME,
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.platform.config.settings import get_settings
from long_invest.platform.errors import AppError
from long_invest.platform.http.request_context import (
    create_request_context,
    reset_request_context,
    set_request_context,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _request(*, origin: str | None = None, csrf: str | None = None) -> Request:
    headers = [(b"cookie", f"{AUTH_COOKIE_NAME}=session-token".encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if csrf is not None:
        headers.append((b"x-csrf-token", csrf.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/test",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "scheme": "https",
            "server": ("test", 443),
        }
    )


class FakeApplication:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id="user-1")
        self.session = SimpleNamespace(id="session-1")
        self.authenticate_arguments: dict | None = None
        self.write_arguments: dict | None = None

    async def authenticate(self, **kwargs):  # type: ignore[no-untyped-def]
        self.authenticate_arguments = kwargs
        return SimpleNamespace(user=self.user, session=self.session)

    async def validate_write_request(self, **kwargs):  # type: ignore[no-untyped-def]
        self.write_arguments = kwargs
        return SimpleNamespace(user=self.user, session=self.session)


@pytest.mark.anyio
async def test_authenticated_dependency_uses_cookie_and_records_identity() -> None:
    context_token = set_request_context(
        create_request_context(
            request_id="req_test",
            client_ip="127.0.0.1",
            route_template="/api/v1/test",
            idempotency_key="idem-test",
        )
    )
    application = FakeApplication()
    try:
        authenticated = await require_authenticated_request(  # type: ignore[arg-type]
            _request(), application
        )
    finally:
        reset_request_context(context_token)

    assert authenticated.user.id == "user-1"
    assert authenticated.session.id == "session-1"
    assert authenticated.audit_context.idempotency_key == "idem-test"
    assert application.authenticate_arguments is not None
    assert application.authenticate_arguments["session_token"] == "session-token"


@pytest.mark.anyio
async def test_write_dependency_requires_origin_cookie_and_csrf(monkeypatch) -> None:
    monkeypatch.setenv("LONGINVEST_AUTH_ALLOWED_ORIGINS", "https://test")
    get_settings.cache_clear()
    context_token = set_request_context(
        create_request_context(
            request_id="req_write",
            client_ip="127.0.0.1",
            route_template="/api/v1/test",
            idempotency_key="idem-write",
        )
    )
    application = FakeApplication()
    try:
        authenticated = await require_verified_write_request(  # type: ignore[arg-type]
            _request(origin="https://test", csrf="csrf-token"), application
        )
    finally:
        reset_request_context(context_token)
        get_settings.cache_clear()

    assert authenticated.session.id == "session-1"
    assert application.write_arguments is not None
    assert application.write_arguments["session_token"] == "session-token"
    assert application.write_arguments["csrf_token"] == "csrf-token"


@pytest.mark.anyio
async def test_write_dependency_rejects_missing_origin_before_application() -> None:
    context_token = set_request_context(
        create_request_context(
            request_id="req_invalid",
            client_ip="127.0.0.1",
            route_template="/api/v1/test",
            idempotency_key=None,
        )
    )
    application = FakeApplication()
    try:
        with pytest.raises(AppError) as caught:
            await require_verified_write_request(  # type: ignore[arg-type]
                _request(csrf="csrf-token"), application
            )
    finally:
        reset_request_context(context_token)

    assert caught.value.code == "AUTH_ORIGIN_INVALID"
    assert application.write_arguments is None


def test_authenticated_dependency_can_be_attached_to_a_fastapi_route() -> None:
    application = FakeApplication()
    app = create_app()
    app.dependency_overrides[get_auth_application] = lambda: application

    @app.get("/_test/protected")
    async def protected(
        authenticated: Annotated[
            AuthenticatedRequest,
            Depends(require_authenticated_request),
        ],
    ) -> dict[str, str]:
        return {"session_id": str(authenticated.session.id)}

    with TestClient(app, base_url="https://test") as client:
        client.cookies.set(AUTH_COOKIE_NAME, "session-token")
        response = client.get("/_test/protected")

    assert response.status_code == 200
    assert response.json() == {"session_id": "session-1"}
