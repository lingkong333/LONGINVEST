from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.securities.api import router
from long_invest.modules.securities.application import get_security_application
from long_invest.modules.securities.models import Security
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import app_error_handler


def security() -> Security:
    return Security(
        id=uuid4(),
        symbol="600000.SH",
        exchange_code="600000",
        name="浦发银行",
        market="SH",
        security_type="A_SHARE",
        listed_on=date(1999, 11, 10),
        delisted_on=None,
        listing_status="LISTED",
        is_st=False,
        is_suspended=False,
        provider_codes={"eastmoney": "1.600000", "sina": "sh600000"},
        master_version=7,
        source="eastmoney",
        source_version="v7",
    )


def client_for(application):
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(router)
    identity = SimpleNamespace(user=SimpleNamespace(id=uuid4()))
    app.dependency_overrides[get_security_application] = lambda: application
    app.dependency_overrides[require_authenticated_request] = lambda: identity
    app.dependency_overrides[require_verified_write_request] = lambda: identity
    return TestClient(app), identity


def test_list_and_search_return_server_pagination() -> None:
    application = Mock()
    application.list = AsyncMock(return_value=([security()], 51))
    application.search = AsyncMock(return_value=([], 0))
    application.allowed_actions = AsyncMock(return_value=())
    client, _identity = client_for(application)

    listed = client.get("/api/v1/securities?page=3&page_size=25")
    searched = client.get(
        "/api/v1/securities/search", params={"q": "浦发", "page": 1, "page_size": 10}
    )

    assert listed.status_code == 200
    assert listed.json()["data"]["pagination"] == {
        "page": 3,
        "page_size": 25,
        "total": 51,
    }
    assert len(listed.json()["data"]["items"]) == 1
    application.list.assert_awaited_once_with(page=3, page_size=25)
    application.search.assert_awaited_once_with(query="浦发", page=1, page_size=10)
    assert searched.json()["data"]["items"] == []


def test_detail_not_found_has_stable_error() -> None:
    application = Mock()
    application.get = AsyncMock(
        side_effect=AppError(
            code="SECURITY_NOT_FOUND", message="股票不存在", status_code=404
        )
    )
    client, _identity = client_for(application)

    response = client.get("/api/v1/securities/000001.SZ")

    assert response.status_code == 404
    assert response.json()["code"] == "SECURITY_NOT_FOUND"


def test_search_rejects_a_whitespace_only_query() -> None:
    application = Mock()
    application.search = AsyncMock()
    client, _identity = client_for(application)

    response = client.get("/api/v1/securities/search", params={"q": "   "})

    assert response.status_code == 422
    assert response.json()["code"] == "SECURITY_SEARCH_QUERY_INVALID"
    application.search.assert_not_awaited()


def test_refresh_requires_confirmation_and_idempotency_key() -> None:
    application = Mock()
    application.refresh = AsyncMock()
    client, _identity = client_for(application)

    unconfirmed = client.post(
        "/api/v1/securities/refresh",
        json={"confirm": False, "reason": "刷新主数据"},
        headers={"Idempotency-Key": "refresh-1"},
    )
    missing_key = client.post(
        "/api/v1/securities/refresh",
        json={"confirm": True, "reason": "刷新主数据"},
    )

    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["code"] == "AUTH_CONFIRMATION_REQUIRED"
    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    application.refresh.assert_not_awaited()


def test_refresh_creates_job_for_verified_identity() -> None:
    job = SimpleNamespace(
        id=uuid4(), status="PENDING_DISPATCH", job_type="SECURITY_MASTER_REFRESH"
    )
    application = Mock()
    application.refresh = AsyncMock(return_value=job)
    client, identity = client_for(application)

    response = client.post(
        "/api/v1/securities/refresh",
        json={"confirm": True, "reason": "刷新主数据"},
        headers={"Idempotency-Key": "refresh-1"},
    )

    assert response.status_code == 202
    assert response.json()["data"]["job_id"] == str(job.id)
    arguments = application.refresh.await_args.kwargs
    assert arguments["idempotency_key"] == "refresh-1"
    assert arguments["created_by_user_id"] == str(identity.user.id)
    assert arguments["reason"] == "刷新主数据"


def test_database_unavailable_error_is_exposed_as_stable_503() -> None:
    application = Mock()
    application.list = AsyncMock(
        side_effect=AppError(
            code="SECURITY_BACKEND_UNAVAILABLE",
            message="股票主数据服务暂时不可用",
            status_code=503,
        )
    )
    client, _identity = client_for(application)

    response = client.get("/api/v1/securities")

    assert response.status_code == 503
    assert response.json()["code"] == "SECURITY_BACKEND_UNAVAILABLE"
