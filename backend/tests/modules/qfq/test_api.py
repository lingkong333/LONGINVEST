from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from long_invest.modules.auth.application import get_auth_application
from long_invest.modules.auth.dependencies import (
    AUTH_COOKIE_NAME,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.qfq.api import router
from long_invest.modules.qfq.application import get_qfq_application
from long_invest.modules.qfq.contracts import (
    Page,
    QfqBarView,
    QfqDatasetLifecycle,
    QfqDatasetView,
    QfqFreshness,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import (
    app_error_handler,
    validation_error_handler,
)
from long_invest.platform.http.middleware import RequestContextMiddleware

NOW = datetime(2026, 7, 16, 8, tzinfo=UTC)


def client_for(application, *, authenticated=True):
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(router)
    identity = SimpleNamespace(
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=uuid4()),
        audit_context=SimpleNamespace(
            request_id="req-qfq-api",
            trusted_ip="127.0.0.1",
        ),
    )
    app.dependency_overrides[get_qfq_application] = lambda: application
    if authenticated:
        app.dependency_overrides[require_authenticated_request] = lambda: identity
        app.dependency_overrides[require_verified_write_request] = lambda: identity
    else:

        def reject():
            raise AppError(
                code="AUTH_SESSION_INVALID", message="未登录", status_code=401
            )

        app.dependency_overrides[require_authenticated_request] = reject
        app.dependency_overrides[require_verified_write_request] = reject
    return TestClient(app), identity, app


def dataset_view() -> QfqDatasetView:
    return QfqDatasetView(
        id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        version=3,
        requested_start=date(2026, 7, 1),
        requested_end=date(2026, 7, 16),
        actual_start=date(2026, 7, 2),
        actual_end=date(2026, 7, 16),
        as_of_date=date(2026, 7, 16),
        provider="eastmoney",
        provider_contract_version="v1",
        anchor_date=date(2026, 7, 16),
        anchor_close="10.25",
        row_count=1,
        checksum="a" * 64,
        lifecycle=QfqDatasetLifecycle.CURRENT,
        freshness=QfqFreshness.FRESH,
        stale_reason=None,
        created_at=NOW,
        activated_at=NOW,
        superseded_at=None,
    )


def bar_page() -> Page[QfqBarView]:
    return Page(
        items=(
            QfqBarView(
                trade_date=date(2026, 7, 16),
                open="10.10",
                high="10.50",
                low="10.00",
                close="10.25",
                volume=100,
                amount="1025.0000",
            ),
        ),
        total=1,
        page=2,
        page_size=20,
    )


def test_get_requires_authentication_and_returns_concrete_page() -> None:
    application = Mock()
    application.get_data = AsyncMock(return_value=(dataset_view(), bar_page()))
    unauthenticated, _, _ = client_for(application, authenticated=False)
    rejected = unauthenticated.get("/api/v1/qfq-data/600000.SH")
    assert rejected.status_code == 401

    client, _, _ = client_for(application)
    response = client.get(
        "/api/v1/qfq-data/600000.SH",
        params={
            "start": "2026-07-01",
            "end": "2026-07-16",
            "page": 2,
            "page_size": 20,
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["dataset"]["version"] == 3
    assert response.json()["data"]["items"][0]["close"] == "10.25"
    assert response.json()["data"]["pagination"] == {
        "page": 2,
        "page_size": 20,
        "total": 1,
    }
    application.get_data.assert_awaited_once_with(
        "600000.SH",
        start=date(2026, 7, 1),
        end=date(2026, 7, 16),
        page=2,
        page_size=20,
    )


def test_refresh_requires_confirmation_reason_and_idempotency_key() -> None:
    application = Mock()
    application.submit_refresh = AsyncMock()
    client, _, _ = client_for(application)
    url = "/api/v1/qfq-data/600000.SH/refresh"

    unconfirmed = client.post(
        url,
        json={
            "start": "2026-07-01",
            "end": "2026-07-16",
            "as_of_date": "2026-07-16",
            "confirm": False,
            "reason": "manual refresh",
        },
        headers={"Idempotency-Key": "qfq-1"},
    )
    blank_reason = client.post(
        url,
        json={
            "start": "2026-07-01",
            "end": "2026-07-16",
            "as_of_date": "2026-07-16",
            "confirm": True,
            "reason": "   ",
        },
        headers={"Idempotency-Key": "qfq-1"},
    )
    missing_key = client.post(
        url,
        json={
            "start": "2026-07-01",
            "end": "2026-07-16",
            "as_of_date": "2026-07-16",
            "confirm": True,
            "reason": "manual refresh",
        },
    )

    assert unconfirmed.json()["code"] == "AUTH_CONFIRMATION_REQUIRED"
    assert blank_reason.json()["code"] == "QFQ_WINDOW_INVALID"
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    application.submit_refresh.assert_not_awaited()


def test_refresh_returns_typed_accepted_job_for_verified_identity() -> None:
    job = SimpleNamespace(id=uuid4(), job_type="QFQ_REFRESH", status="PENDING_DISPATCH")
    application = Mock()
    application.submit_refresh = AsyncMock(return_value=job)
    client, identity, _ = client_for(application)

    response = client.post(
        "/api/v1/qfq-data/600000.SH/refresh",
        json={
            "start": "2026-07-01",
            "end": "2026-07-16",
            "as_of_date": "2026-07-16",
            "confirm": True,
            "reason": " manual refresh ",
        },
        headers={"Idempotency-Key": "qfq-1"},
    )

    assert response.status_code == 202
    assert response.json()["code"] == "JOB_ACCEPTED"
    assert response.json()["data"] == {
        "job_id": str(job.id),
        "job_type": "QFQ_REFRESH",
        "status": "PENDING_DISPATCH",
    }
    arguments = application.submit_refresh.await_args.kwargs
    assert arguments["actor_user_id"] == str(identity.user.id)
    assert arguments["session_id"] == str(identity.session.id)
    assert arguments["reason"] == "manual refresh"
    assert arguments["idempotency_key"] == "qfq-1"


def test_refresh_exposes_stable_conflict() -> None:
    application = Mock()
    application.submit_refresh = AsyncMock(
        side_effect=AppError(
            code="QFQ_REFRESH_CONFLICT",
            message="different content",
            status_code=409,
        )
    )
    client, _, _ = client_for(application)

    response = client.post(
        "/api/v1/qfq-data/600000.SH/refresh",
        json={
            "start": "2026-07-01",
            "end": "2026-07-16",
            "as_of_date": "2026-07-16",
            "confirm": True,
            "reason": "manual refresh",
        },
        headers={"Idempotency-Key": "qfq-conflict"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "QFQ_REFRESH_CONFLICT"


def test_openapi_declares_required_header_and_concrete_success_models() -> None:
    application = Mock()
    _, _, app = client_for(application)
    schema = app.openapi()
    get_operation = schema["paths"]["/api/v1/qfq-data/{symbol}"]["get"]
    post_operation = schema["paths"]["/api/v1/qfq-data/{symbol}/refresh"]["post"]

    header = next(
        item
        for item in post_operation["parameters"]
        if item["name"] == "Idempotency-Key"
    )
    assert header["required"] is True
    assert (
        get_operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == "#/components/schemas/QfqDataResponse"
    )
    assert (
        post_operation["responses"]["202"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == "#/components/schemas/QfqJobResponse"
    )


def test_refresh_uses_origin_session_and_csrf_protection() -> None:
    application = Mock()
    application.submit_refresh = AsyncMock(
        return_value=SimpleNamespace(
            id=uuid4(), job_type="QFQ_REFRESH", status="PENDING_DISPATCH"
        )
    )
    user_id, session_id = uuid4(), uuid4()

    class FakeAuthApplication:
        async def validate_write_request(self, **_kwargs):
            return SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                session=SimpleNamespace(id=session_id),
            )

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(router)
    app.dependency_overrides[get_qfq_application] = lambda: application
    app.dependency_overrides[get_auth_application] = lambda: FakeAuthApplication()
    client = TestClient(app)
    body = {
        "start": "2026-07-01",
        "end": "2026-07-16",
        "as_of_date": "2026-07-16",
        "confirm": True,
        "reason": "manual refresh",
    }
    headers = {
        "Origin": "http://127.0.0.1:15173",
        "X-CSRF-Token": "csrf-token",
        "Idempotency-Key": "qfq-auth",
        "Cookie": f"{AUTH_COOKIE_NAME}=session-token",
    }
    url = "/api/v1/qfq-data/600000.SH/refresh"

    no_session = client.post(
        url,
        json=body,
        headers={key: value for key, value in headers.items() if key != "Cookie"},
    )
    no_origin = client.post(
        url,
        json=body,
        headers={key: value for key, value in headers.items() if key != "Origin"},
    )
    no_csrf = client.post(
        url,
        json=body,
        headers={key: value for key, value in headers.items() if key != "X-CSRF-Token"},
    )
    accepted = client.post(url, json=body, headers=headers)

    assert no_session.status_code == 401
    assert no_origin.status_code == 403
    assert no_csrf.status_code == 403
    assert accepted.status_code == 202
    arguments = application.submit_refresh.await_args.kwargs
    assert arguments["actor_user_id"] == str(user_id)
    assert arguments["session_id"] == str(session_id)
