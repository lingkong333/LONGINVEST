from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from long_invest.bootstrap.app import create_app
from long_invest.modules.auth.api import (
    AUTH_COOKIE_NAME,
    clear_session_cookie,
    require_confirmation,
    resolve_client_ip,
    router,
    set_session_cookie,
    validate_browser_origin,
)
from long_invest.modules.auth.application import get_auth_application
from long_invest.modules.auth.contracts import SessionStatus, UserStatus
from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.modules.auth.service import LoginResult
from long_invest.modules.auth.tokens import (
    CsrfCredentials,
    SessionCredentials,
    TokenService,
)
from long_invest.platform.config.settings import get_settings
from long_invest.platform.errors import AppError


def test_auth_router_exposes_only_the_v31_session_endpoints() -> None:
    routes = {
        (method, route.path) for route in router.routes for method in route.methods
    }

    assert routes == {
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/auth/csrf"),
        ("POST", "/api/v1/auth/activity"),
        ("GET", "/api/v1/auth/sessions"),
        ("POST", "/api/v1/auth/sessions/{session_id}/revoke"),
        ("POST", "/api/v1/auth/sessions/revoke-others"),
        ("POST", "/api/v1/auth/sessions/revoke-all"),
        ("POST", "/api/v1/auth/change-password"),
    }


def test_origin_validation_accepts_only_an_explicit_origin_or_referer() -> None:
    allowed = ("http://127.0.0.1:15173", "https://invest.example.com")

    assert validate_browser_origin(
        origin="http://127.0.0.1:15173",
        referer=None,
        allowed_origins=allowed,
    )
    assert validate_browser_origin(
        origin=None,
        referer="https://invest.example.com/settings/profile",
        allowed_origins=allowed,
    )
    assert not validate_browser_origin(
        origin="https://evil.example.com",
        referer=None,
        allowed_origins=allowed,
    )
    assert not validate_browser_origin(
        origin=None,
        referer=None,
        allowed_origins=allowed,
    )


def test_forwarded_ip_is_trusted_only_from_an_explicit_proxy_network() -> None:
    trusted = ("172.16.0.0/12", "127.0.0.0/8")

    assert (
        resolve_client_ip(
            peer_ip="172.20.0.4",
            forwarded_ip="203.0.113.9",
            trusted_proxy_networks=trusted,
        )
        == "203.0.113.9"
    )
    assert (
        resolve_client_ip(
            peer_ip="198.51.100.7",
            forwarded_ip="203.0.113.9",
            trusted_proxy_networks=trusted,
        )
        == "198.51.100.7"
    )
    assert (
        resolve_client_ip(
            peer_ip="172.20.0.4",
            forwarded_ip="not-an-ip",
            trusted_proxy_networks=trusted,
        )
        == "172.20.0.4"
    )


def test_destructive_session_actions_require_explicit_confirmation() -> None:
    with pytest.raises(AppError) as caught:
        require_confirmation(False)

    assert caught.value.code == "AUTH_CONFIRMATION_REQUIRED"


def test_session_cookie_is_host_only_secure_and_http_only() -> None:
    response = Response()

    set_session_cookie(response, "secret-session-token")

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{AUTH_COOKIE_NAME}=secret-session-token;")
    assert "Domain=" not in cookie
    assert "HttpOnly" in cookie
    assert "Path=/" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie


def test_clearing_session_cookie_preserves_security_attributes() -> None:
    response = Response()

    clear_session_cookie(response)

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f'{AUTH_COOKIE_NAME}="";')
    assert "Max-Age=0" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie


class FakeAuthApplication:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.user = AppUser(
            id=uuid4(),
            username="admin",
            password_hash="unused",
            password_version=1,
            status=UserStatus.ACTIVE,
            created_at=now,
            password_changed_at=now,
        )
        self.session = UserSession(
            id=uuid4(),
            user_id=self.user.id,
            token_digest=TokenService.digest("session-secret"),
            csrf_secret_digest=TokenService.digest("csrf-secret"),
            password_version=1,
            created_at=now,
            last_request_at=now,
            last_user_activity_at=now,
            idle_expires_at=now + timedelta(days=30),
            absolute_expires_at=now + timedelta(days=90),
            status=SessionStatus.ACTIVE,
        )
        self.logout_calls = 0
        self.last_logout_arguments: dict | None = None

    async def login(self, **_kwargs) -> LoginResult:
        return LoginResult(
            session=self.session,
            credentials=SessionCredentials(
                session_token="session-secret",
                csrf_token="initial-csrf-secret",
                token_digest=self.session.token_digest,
                csrf_digest=self.session.csrf_secret_digest,
            ),
        )

    async def issue_csrf(self, **_kwargs) -> CsrfCredentials:
        return CsrfCredentials(
            csrf_token="csrf-secret",
            csrf_digest=self.session.csrf_secret_digest,
        )

    async def logout(self, **kwargs) -> bool:
        self.logout_calls += 1
        self.last_logout_arguments = kwargs
        return True


def test_login_csrf_and_logout_use_the_secure_cookie_flow(monkeypatch) -> None:
    monkeypatch.setenv(
        "LONGINVEST_AUTH_ALLOWED_ORIGINS",
        "https://127.0.0.1:15173",
    )
    get_settings.cache_clear()
    get_auth_application.cache_clear()
    fake = FakeAuthApplication()
    app = create_app()
    app.dependency_overrides[get_auth_application] = lambda: fake
    try:
        with TestClient(app, base_url="https://127.0.0.1:15173") as client:
            login = client.post(
                "/api/v1/auth/login",
                headers={"Origin": "https://127.0.0.1:15173"},
                json={"username": "admin", "password": "valid password"},
            )
            csrf = client.get("/api/v1/auth/csrf")
            logout = client.post(
                "/api/v1/auth/logout",
                headers={
                    "Origin": "https://127.0.0.1:15173",
                    "X-CSRF-Token": "csrf-secret",
                },
            )
    finally:
        get_settings.cache_clear()
        get_auth_application.cache_clear()

    assert login.status_code == 200
    assert login.json()["data"]["session_id"] == str(fake.session.id)
    assert csrf.status_code == 200
    assert csrf.json()["data"] == {"csrf_token": "csrf-secret"}
    assert logout.status_code == 200
    assert fake.logout_calls == 1
    assert AUTH_COOKIE_NAME not in logout.cookies


def test_login_rejects_a_missing_browser_origin() -> None:
    app = create_app()
    app.dependency_overrides[get_auth_application] = lambda: FakeAuthApplication()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "valid password"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_ORIGIN_INVALID"


def test_logout_replay_requires_the_original_cookie_and_csrf(monkeypatch) -> None:
    monkeypatch.setenv(
        "LONGINVEST_AUTH_ALLOWED_ORIGINS",
        "https://127.0.0.1:15173",
    )
    get_settings.cache_clear()
    fake = FakeAuthApplication()
    app = create_app()
    app.dependency_overrides[get_auth_application] = lambda: fake
    try:
        with TestClient(app, base_url="https://127.0.0.1:15173") as client:
            client.cookies.set(AUTH_COOKIE_NAME, "original-session-token")
            response = client.post(
                "/api/v1/auth/logout",
                headers={
                    "Origin": "https://127.0.0.1:15173",
                    "Idempotency-Key": "original-logout-request",
                    "X-CSRF-Token": "original-csrf-token",
                },
            )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert fake.last_logout_arguments is not None
    assert fake.last_logout_arguments["session_token"] == "original-session-token"
    assert fake.last_logout_arguments["csrf_token"] == "original-csrf-token"
