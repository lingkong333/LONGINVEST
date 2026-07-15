from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.dependencies import (
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


def _client(application, *, authenticated=True):
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(router)
    identity = SimpleNamespace(user=SimpleNamespace(id=uuid4()))
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
    response = client.get("/api/v1/daily-data/batches")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_SESSION_INVALID"


def test_batch_and_missing_pages_are_exposed() -> None:
    batch_id = uuid4()
    application = Mock()
    application.list_batches = AsyncMock(return_value=([], 0))
    application.list_missing = AsyncMock(return_value=([], 0))
    client, _ = _client(application)
    batches = client.get("/api/v1/daily-data/batches?page=2&page_size=20")
    missing = client.get(
        f"/api/v1/daily-data/batches/{batch_id}/missing?page=1&page_size=10"
    )
    assert batches.status_code == 200
    assert batches.json()["data"]["pagination"]["page"] == 2
    assert missing.status_code == 200
    application.list_missing.assert_awaited_once_with(batch_id, page=1, page_size=10)


def test_retry_requires_confirmation_and_idempotency_key() -> None:
    application = Mock()
    application.retry = AsyncMock()
    client, _ = _client(application)
    unconfirmed = client.post(
        f"/api/v1/daily-data/batches/{uuid4()}/retry",
        json={"confirm": False},
        headers={"Idempotency-Key": "retry-1"},
    )
    missing_key = client.post(
        f"/api/v1/daily-data/batches/{uuid4()}/retry", json={"confirm": True}
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
        json={"confirm": True},
        headers={"Idempotency-Key": "retry-1"},
    )
    assert response.status_code == 202
    assert response.json()["data"]["job_type"] == "DAILY_DATA_RETRY"
    application.retry.assert_awaited_once()
    kwargs = application.retry.await_args.kwargs
    assert kwargs["batch_id"] == batch_id
    assert kwargs["created_by_user_id"] == str(identity.user.id)


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
