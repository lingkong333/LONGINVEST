from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from long_invest.bootstrap.app import create_app
from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.calendar.api import (
    _calendar_actions,
    _calendar_context,
    _day_data,
    _result_data,
    _version_data,
    get_calendar_service,
    router,
)
from long_invest.modules.calendar.contracts import (
    CalendarValidationIssue,
    CalendarVersionResult,
)
from long_invest.platform.errors import AppError


def test_calendar_router_exposes_exactly_the_nine_v31_routes() -> None:
    routes = {
        (method, route.path) for route in router.routes for method in route.methods
    }

    assert routes == {
        ("GET", "/api/v1/trading-calendar"),
        ("GET", "/api/v1/trading-calendar/{date}"),
        ("GET", "/api/v1/trading-calendar/coverage"),
        ("GET", "/api/v1/trading-calendar/next-trading-day"),
        ("GET", "/api/v1/trading-calendar/previous-trading-day"),
        ("PATCH", "/api/v1/trading-calendar/{date}"),
        ("POST", "/api/v1/trading-calendar/import"),
        ("GET", "/api/v1/trading-calendar/versions"),
        ("POST", "/api/v1/trading-calendar/versions/{version_id}/restore"),
    }


def test_read_and_write_routes_use_the_published_auth_dependencies() -> None:
    for route in router.routes:
        dependency_calls = {item.call for item in route.dependant.dependencies}
        if "GET" in route.methods:
            assert require_authenticated_request in dependency_calls
        else:
            assert require_verified_write_request in dependency_calls


def test_write_schemas_require_confirm_reason_and_idempotency_header() -> None:
    for route in router.routes:
        if route.methods & {"POST", "PATCH"}:
            dependency_names = {
                field.name for field in route.dependant.header_params
            }
            assert "idempotency_key" in dependency_names


def test_api_builds_complete_audit_context_from_verified_identity() -> None:
    authenticated = SimpleNamespace(
        user=SimpleNamespace(id="user-1"),
        session=SimpleNamespace(id="session-1"),
        audit_context=SimpleNamespace(
            request_id="req-real",
            trusted_ip="203.0.113.7",
        ),
    )

    context = _calendar_context(authenticated, "idem-real")

    assert context.model_dump() == {
        "request_id": "req-real",
        "idempotency_key": "idem-real",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "203.0.113.7",
    }


def test_invalid_calendar_result_becomes_http_422_with_every_issue() -> None:
    result = CalendarVersionResult(
        issues=(
            CalendarValidationIssue(code="ONE", path="days[0]", message="一"),
            CalendarValidationIssue(code="TWO", path="days[1]", message="二"),
        )
    )

    with pytest.raises(AppError) as caught:
        _result_data(result)

    assert caught.value.status_code == 422
    assert caught.value.code == "CALENDAR_CONTENT_INVALID"
    assert [item["code"] for item in caught.value.details["issues"]] == [
        "ONE",
        "TWO",
    ]


@pytest.mark.anyio
async def test_database_failure_is_mapped_to_stable_503(monkeypatch) -> None:
    class BrokenTransaction:
        async def __aenter__(self):
            raise SQLAlchemyError("database unavailable")

        async def __aexit__(self, *_args):
            return False

    database = SimpleNamespace(transaction=lambda: BrokenTransaction())
    monkeypatch.setattr(
        "long_invest.modules.calendar.api.get_database", lambda: database
    )
    dependency = get_calendar_service()

    with pytest.raises(AppError) as caught:
        await anext(dependency)

    assert caught.value.code == "CALENDAR_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503


@pytest.mark.anyio
async def test_repository_audit_and_outbox_share_the_api_transaction(
    monkeypatch,
) -> None:
    session = SimpleNamespace()

    class Transaction:
        exited = False

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            self.exited = True
            return False

    transaction = Transaction()
    database = SimpleNamespace(transaction=lambda: transaction)
    monkeypatch.setattr(
        "long_invest.modules.calendar.api.get_database", lambda: database
    )
    dependency = get_calendar_service()

    service = await anext(dependency)

    assert service._repository._session is session
    assert service._audit._repository._session is session
    assert service._events._session is session
    await dependency.aclose()
    assert transaction.exited is True


def test_invalid_import_is_returned_as_http_422_with_all_issues() -> None:
    class Service:
        async def import_version(self, _command):
            return CalendarVersionResult(
                issues=(
                    CalendarValidationIssue(
                        code="ONE", path="days[0]", message="一"
                    ),
                    CalendarValidationIssue(
                        code="TWO", path="days[1]", message="二"
                    ),
                )
            )

    authenticated = SimpleNamespace(
        user=SimpleNamespace(id="user-1"),
        session=SimpleNamespace(id="session-1"),
        audit_context=SimpleNamespace(
            request_id="req-http",
            trusted_ip="127.0.0.1",
        ),
    )
    app = create_app()
    app.include_router(router)
    app.dependency_overrides[get_calendar_service] = lambda: Service()
    app.dependency_overrides[require_verified_write_request] = (
        lambda: authenticated
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/trading-calendar/import",
            headers={"Idempotency-Key": "idem-http"},
            json={
                "market": "CN_A",
                "source": "git",
                "source_version": "bad",
                "reason": "测试非法导入",
                "confirm": True,
                "days": [
                    {
                        "trade_date": "2026-07-15",
                        "is_trading_day": True,
                        "status": "CONFIRMED",
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "CALENDAR_CONTENT_INVALID"
    assert [
        item["code"] for item in response.json()["details"]["issues"]
    ] == ["ONE", "TWO"]


def test_calendar_views_expose_backend_allowed_actions() -> None:
    day = SimpleNamespace(
        trade_date="2026-07-23",
        is_trading_day=True,
        status="CONFIRMED",
        source="SSE",
        note=None,
        override_reason=None,
        sessions=(),
    )
    version = SimpleNamespace(
        id="version-1",
        market="CN_A",
        version_number=2,
        source="SSE",
        source_version="2026",
        based_on_version_id=None,
        reason="年度日历",
        created_at="2026-01-01T00:00:00Z",
    )

    assert _calendar_actions() == ["IMPORT", "OVERRIDE"]
    assert _day_data(day)["allowed_actions"] == ["OVERRIDE"]
    assert _version_data(version, is_current=False)["allowed_actions"] == [
        "RESTORE"
    ]
    assert _version_data(version, is_current=True)["allowed_actions"] == []
