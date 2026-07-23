from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.application import get_auth_application
from long_invest.modules.auth.dependencies import (
    AUTH_COOKIE_NAME,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.daily_data.api import router
from long_invest.modules.daily_data.application import get_daily_data_application
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import (
    app_error_handler,
    validation_error_handler,
)
from long_invest.platform.http.middleware import RequestContextMiddleware


def _client(application, *, authenticated=True):
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(router)
    identity = SimpleNamespace(
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=uuid4()),
        audit_context=SimpleNamespace(
            request_id="req_12345678",
            trusted_ip="127.0.0.1",
        ),
    )
    app.dependency_overrides[get_daily_data_application] = lambda: application
    if authenticated:
        app.dependency_overrides[require_authenticated_request] = lambda: identity
        app.dependency_overrides[require_verified_write_request] = lambda: identity
    else:

        def reject():
            raise AppError(
                code="AUTH_SESSION_INVALID", message="未登录", status_code=401
            )

        app.dependency_overrides[require_authenticated_request] = reject
    return TestClient(app), identity


def test_batch_and_missing_reads_require_authentication() -> None:
    application = Mock()
    application.list_batches = AsyncMock()
    client, _ = _client(application, authenticated=False)
    batch_id = uuid4()
    urls = (
        "/api/v1/daily-data/batches",
        f"/api/v1/daily-data/batches/{batch_id}/missing",
        "/api/v1/daily-bars/600000.SH?start=2026-07-01&end=2026-07-15",
        "/api/v1/daily-bars/600000.SH/revisions",
    )

    for url in urls:
        response = client.get(url)
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_SESSION_INVALID"


def test_batch_and_missing_pages_are_exposed() -> None:
    batch_id = uuid4()
    snapshot_id = uuid4()
    security_id = uuid4()
    now = datetime(2026, 7, 15, 17, tzinfo=UTC)
    batch = SimpleNamespace(
        id=batch_id,
        trading_date=date(2026, 7, 15),
        universe_snapshot_id=snapshot_id,
        parent_batch_id=None,
        symbols=["600000.SH"],
        security_ids=[str(security_id)],
        known_corporate_action_symbols=[],
        idempotency_key="daily-20260715",
        status="SUCCEEDED",
        expected_count=1,
        fetched_count=1,
        validated_count=1,
        committed_count=1,
        missing_count=0,
        failed_count=0,
        created_at=now,
        started_at=now,
        deadline_at=now,
        completed_at=now,
    )
    missing_item = SimpleNamespace(
        id=uuid4(),
        batch_id=batch_id,
        security_id=security_id,
        symbol="600000.SH",
        reason="SUSPENDED",
        error_code=None,
        explained=True,
        created_at=now,
    )
    application = Mock()
    application.list_batches = AsyncMock(return_value=([batch], 1))
    application.list_missing = AsyncMock(return_value=([missing_item], 1))
    application.allowed_actions = lambda _batch: ()
    client, _ = _client(application)
    batches = client.get("/api/v1/daily-data/batches?page=2&page_size=20")
    missing = client.get(
        f"/api/v1/daily-data/batches/{batch_id}/missing?page=1&page_size=10"
    )
    assert batches.status_code == 200
    assert batches.json()["data"]["pagination"]["page"] == 2
    assert batches.json()["data"]["items"][0]["id"] == str(batch_id)
    assert missing.status_code == 200
    assert missing.json()["data"]["items"][0]["symbol"] == "600000.SH"
    application.list_missing.assert_awaited_once_with(batch_id, page=1, page_size=10)


def test_retry_requires_confirmation_and_idempotency_key() -> None:
    application = Mock()
    application.retry = AsyncMock()
    client, _ = _client(application)
    unconfirmed = client.post(
        f"/api/v1/daily-data/batches/{uuid4()}/retry",
        json={"confirm": False, "reason": "manual retry"},
        headers={"Idempotency-Key": "retry-1"},
    )
    missing_key = client.post(
        f"/api/v1/daily-data/batches/{uuid4()}/retry",
        json={"confirm": True, "reason": "manual retry"},
    )
    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["code"] == "AUTH_CONFIRMATION_REQUIRED"
    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    application.retry.assert_not_awaited()


def test_retry_only_submits_daily_retry_job() -> None:
    batch_id = uuid4()
    job = SimpleNamespace(
        id=uuid4(), job_type="DAILY_DATA_RETRY", status="PENDING_DISPATCH"
    )
    application = Mock()
    application.retry = AsyncMock(return_value=job)
    client, identity = _client(application)
    response = client.post(
        f"/api/v1/daily-data/batches/{batch_id}/retry",
        json={"confirm": True, "reason": "manual retry"},
        headers={"Idempotency-Key": "retry-1"},
    )
    assert response.status_code == 202
    assert response.json()["code"] == "JOB_ACCEPTED"
    assert response.json()["data"]["job_type"] == "DAILY_DATA_RETRY"
    application.retry.assert_awaited_once()
    kwargs = application.retry.await_args.kwargs
    assert kwargs["batch_id"] == batch_id
    assert kwargs["audit_context"].actor_user_id == str(identity.user.id)
    assert kwargs["audit_context"].reason == "manual retry"


def test_retry_non_retryable_batch_returns_stable_conflict() -> None:
    batch_id = uuid4()
    application = Mock()
    application.retry = AsyncMock(
        side_effect=AppError(
            code="DAILY_RETRY_STATE_CONFLICT",
            message="batch is not retryable",
            status_code=409,
            details={"status": "SUCCEEDED"},
        )
    )
    client, _ = _client(application)

    response = client.post(
        f"/api/v1/daily-data/batches/{batch_id}/retry",
        json={"confirm": True, "reason": "manual retry"},
        headers={"Idempotency-Key": "retry-1"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "DAILY_RETRY_STATE_CONFLICT"
    assert response.json()["details"] == {"status": "SUCCEEDED"}


def test_retry_uses_real_origin_session_and_csrf_protection() -> None:
    application = Mock()
    application.retry = AsyncMock(
        return_value=SimpleNamespace(
            id=uuid4(), job_type="DAILY_DATA_RETRY", status="PENDING_DISPATCH"
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
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(router)
    app.dependency_overrides[get_daily_data_application] = lambda: application
    app.dependency_overrides[get_auth_application] = lambda: FakeAuthApplication()
    client = TestClient(app)
    url = f"/api/v1/daily-data/batches/{uuid4()}/retry"
    body = {"confirm": True, "reason": "manual retry"}
    valid_headers = {
        "Origin": "http://127.0.0.1:15173",
        "X-CSRF-Token": "csrf-token",
        "Idempotency-Key": "retry-1",
        "Cookie": f"{AUTH_COOKIE_NAME}=session-token",
    }

    unauthenticated = client.post(
        url,
        json=body,
        headers={key: value for key, value in valid_headers.items() if key != "Cookie"},
    )
    missing_origin = client.post(
        url,
        json=body,
        headers={key: value for key, value in valid_headers.items() if key != "Origin"},
    )
    missing_csrf = client.post(
        url,
        json=body,
        headers={
            key: value for key, value in valid_headers.items() if key != "X-CSRF-Token"
        },
    )
    unconfirmed = client.post(
        url,
        json={"confirm": False, "reason": "manual retry"},
        headers=valid_headers,
    )
    missing_key = client.post(
        url,
        json=body,
        headers={
            key: value
            for key, value in valid_headers.items()
            if key != "Idempotency-Key"
        },
    )
    accepted = client.post(url, json=body, headers=valid_headers)

    assert unauthenticated.status_code == 401
    assert missing_origin.status_code == 403
    assert missing_csrf.status_code == 403
    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["code"] == "AUTH_CONFIRMATION_REQUIRED"
    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert accepted.status_code == 202
    context = application.retry.await_args.kwargs["audit_context"]
    assert context.actor_user_id == str(user_id)
    assert context.session_id == str(session_id)
    assert context.idempotency_key == "retry-1"
    assert context.reason == "manual retry"


def test_daily_bars_require_date_range_and_enforce_page_limit() -> None:
    application = Mock()
    application.list_bars = AsyncMock(return_value=([], 0))
    client, _ = _client(application)
    missing_range = client.get("/api/v1/daily-bars/600000.SH")
    too_many = client.get(
        "/api/v1/daily-bars/600000.SH",
        params={"start": "2026-01-01", "end": "2026-07-15", "page_size": 501},
    )
    assert missing_range.status_code == 422
    assert too_many.status_code == 422
    assert application.list_bars.await_count == 0


def test_daily_bars_and_revisions_are_paginated() -> None:
    now = datetime(2026, 7, 15, 17, tzinfo=UTC)
    bar = SimpleNamespace(
        security_id=uuid4(),
        symbol="600000.SH",
        trade_date=date(2026, 7, 15),
        open=10,
        high=11,
        low=9,
        close=10.5,
        previous_close=10,
        volume=100,
        amount=1000,
        source="EASTMONEY",
        data_version=1,
        created_at=now,
        updated_at=now,
    )
    revision = SimpleNamespace(
        id=uuid4(),
        daily_bar_security_id=bar.security_id,
        daily_bar_trade_date=bar.trade_date,
        symbol=bar.symbol,
        revision_no=1,
        old_values={"close": "10"},
        new_values={"close": "10.5"},
        changed_fields=["close"],
        source="EASTMONEY",
        reason="changed",
        created_at=now,
    )
    application = Mock()
    application.list_bars = AsyncMock(return_value=([bar], 1))
    application.list_revisions = AsyncMock(return_value=([revision], 1))
    client, _ = _client(application)
    bars = client.get(
        "/api/v1/daily-bars/600000.SH",
        params={"start": "2026-07-01", "end": "2026-07-15"},
    )
    revisions = client.get("/api/v1/daily-bars/600000.SH/revisions")
    assert bars.status_code == 200
    assert bars.json()["data"]["items"][0]["trade_date"] == "2026-07-15"
    assert revisions.status_code == 200
    assert revisions.json()["data"]["items"][0]["revision_no"] == 1
