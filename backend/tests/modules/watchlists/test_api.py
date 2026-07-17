from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from long_invest.modules.auth.application import get_auth_application
from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.watchlists.api import get_watchlist_application, router
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import register_exception_handlers
from long_invest.platform.http.middleware import RequestContextMiddleware


class Application:
    async def list(self, *, owner_user_id):
        return ()


def app(write_override=None):
    value = FastAPI()
    value.add_middleware(RequestContextMiddleware)
    register_exception_handlers(value)
    value.include_router(router)
    user = type(
        "Auth",
        (),
        {
            "user": type("User", (), {"id": uuid4()})(),
            "session": type("Session", (), {"id": uuid4()})(),
            "audit_context": type(
                "Audit", (), {"request_id": "r", "trusted_ip": "127.0.0.1"}
            )(),
        },
    )()
    value.dependency_overrides[require_authenticated_request] = lambda: user
    if write_override is not None:
        value.dependency_overrides[require_verified_write_request] = write_override
    value.dependency_overrides[get_watchlist_application] = lambda: Application()
    return value


@pytest.mark.anyio
async def test_reads_require_login_and_writes_expose_security_dependencies():
    unauthenticated = FastAPI()
    register_exception_handlers(unauthenticated)
    unauthenticated.include_router(router)

    async def reject_login():
        raise AppError(code="AUTH_SESSION_INVALID", message="login", status_code=401)

    unauthenticated.dependency_overrides[require_authenticated_request] = reject_login
    unauthenticated.dependency_overrides[get_watchlist_application] = lambda: (
        Application()
    )
    async with AsyncClient(
        transport=ASGITransport(app=unauthenticated), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/watchlists")
    assert response.status_code == 401

    paths = app().openapi()["paths"]
    create = paths["/api/v1/watchlists"]["post"]
    headers = {item["name"] for item in create["parameters"] if item["in"] == "header"}
    assert "Idempotency-Key" in headers
    assert create["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("WatchlistResponse")


@pytest.mark.anyio
async def test_write_without_verified_identity_is_rejected_before_application():
    async with AsyncClient(
        transport=ASGITransport(app=app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/watchlists",
            headers={"Idempotency-Key": "one"},
            json={
                "name": "观察",
                "description": None,
                "display_order": 0,
                "reason": "创建",
            },
        )
    assert response.status_code in {401, 403}


@pytest.mark.anyio
async def test_blank_idempotency_header_is_rejected_with_422():
    async def verified():
        return type("Auth", (), {})()

    async with AsyncClient(
        transport=ASGITransport(app=app(verified)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/watchlists",
            headers={"Idempotency-Key": "   "},
            json={
                "name": "观察",
                "description": None,
                "display_order": 0,
                "reason": "创建",
            },
        )
    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.anyio
async def test_trusted_origin_without_csrf_is_rejected_before_application():
    value = app()

    class AuthApplication:
        async def validate_write_request(self, **values):
            raise AssertionError("missing CSRF must be rejected before authentication")

    value.dependency_overrides[get_auth_application] = lambda: AuthApplication()
    async with AsyncClient(
        transport=ASGITransport(app=value),
        base_url="http://test",
        cookies={"__Host-session": "session-token"},
    ) as client:
        response = await client.post(
            "/api/v1/watchlists",
            headers={
                "Idempotency-Key": "one",
                "Origin": "http://127.0.0.1:15173",
            },
            json={
                "name": "观察",
                "description": None,
                "display_order": 0,
                "reason": "创建",
            },
        )
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_CSRF_INVALID"
