from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.positions.contracts import PositionStatus
from long_invest.modules.signals.api import get_signal_application, router
from long_invest.modules.signals.contracts import (
    EvaluationReason,
    EvaluationResult,
    NotificationClass,
    SignalEvaluationView,
    SignalEventView,
    SignalStateView,
    SignalZone,
)
from long_invest.modules.targets.contracts import TargetValues
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import register_exception_handlers

NOW = datetime(2026, 7, 21, 9, tzinfo=UTC)
SUBSCRIPTION_ID = uuid4()
EVALUATION_ID = uuid4()
EVENT_ID = uuid4()
TARGET_REVISION_ID = uuid4()


def _identity():
    return SimpleNamespace(
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=uuid4()),
        audit_context=SimpleNamespace(
            request_id="req-signal-api",
            trusted_ip="127.0.0.1",
        ),
    )


def _targets() -> TargetValues:
    return TargetValues(
        low_strong="8",
        low_watch="9",
        high_watch="12",
        high_strong="13",
    )


def _state() -> SignalStateView:
    return SignalStateView(
        subscription_id=SUBSCRIPTION_ID,
        zone=SignalZone.HIGH,
        version=3,
        last_price=Decimal("12.500000"),
        last_price_at=NOW,
        last_subscription_version=2,
        last_price_version=5,
        last_target_revision_id=TARGET_REVISION_ID,
        last_target_version=4,
        last_position_version=1,
    )


def _evaluation() -> SignalEvaluationView:
    return SignalEvaluationView(
        id=EVALUATION_ID,
        subscription_id=SUBSCRIPTION_ID,
        reason=EvaluationReason.MANUAL_CHECK,
        result=EvaluationResult.APPLIED,
        before_zone=SignalZone.NORMAL,
        after_zone=SignalZone.HIGH,
        subscription_version=2,
        target_revision_id=TARGET_REVISION_ID,
        target_version=4,
        target_date=date(2026, 7, 21),
        targets=_targets(),
        position_status=PositionStatus.HOLDING,
        position_version=1,
        price=Decimal("12.500000"),
        price_at=NOW,
        price_version=5,
        hysteresis_applied=False,
        used_stale_target=False,
        content_hash="a" * 64,
        created_at=NOW,
    )


def _event() -> SignalEventView:
    return SignalEventView(
        id=EVENT_ID,
        subscription_id=SUBSCRIPTION_ID,
        evaluation_id=EVALUATION_ID,
        before_zone=SignalZone.NORMAL,
        after_zone=SignalZone.HIGH,
        reason=EvaluationReason.MANUAL_CHECK,
        price=Decimal("12.500000"),
        price_at=NOW,
        targets=_targets(),
        target_revision_id=TARGET_REVISION_ID,
        target_version=4,
        target_date=date(2026, 7, 21),
        position_status=PositionStatus.HOLDING,
        position_version=1,
        used_stale_target=False,
        state_version=3,
        notification_class=NotificationClass.HIGH,
        notification_eligible=True,
        created_at=NOW,
    )


def _application():
    return SimpleNamespace(
        list_states=AsyncMock(return_value=((_state(),), 41)),
        get_state=AsyncMock(return_value=_state()),
        list_events=AsyncMock(return_value=((_event(),), 21)),
        get_event=AsyncMock(return_value=_event()),
        list_evaluations=AsyncMock(return_value=((_evaluation(),), 16)),
        get_evaluation=AsyncMock(return_value=_evaluation()),
    )


def _app(application=None, *, authenticated: bool = True) -> FastAPI:
    value = FastAPI()
    register_exception_handlers(value)
    value.include_router(router)
    value.dependency_overrides[get_signal_application] = lambda: (
        application or _application()
    )
    if authenticated:
        identity = _identity()
        value.dependency_overrides[require_authenticated_request] = lambda: identity
        value.dependency_overrides[require_verified_write_request] = lambda: identity
    else:

        async def reject():
            raise AppError(code="AUTH_REQUIRED", message="login", status_code=401)

        value.dependency_overrides[require_authenticated_request] = reject
        value.dependency_overrides[require_verified_write_request] = reject
    return value


def test_signal_read_router_freezes_routes_auth_and_response_models() -> None:
    routes = {
        (method, route.path): route
        for route in router.routes
        for method in route.methods
        if method == "GET"
    }
    assert set(routes) == {
        ("GET", "/api/v1/signals/states"),
        ("GET", "/api/v1/signals/states/{subscription_id}"),
        ("GET", "/api/v1/signal-events"),
        ("GET", "/api/v1/signal-events/{event_id}"),
        ("GET", "/api/v1/signal-evaluations"),
        ("GET", "/api/v1/signal-evaluations/{evaluation_id}"),
    }
    for route in routes.values():
        dependencies = {item.call for item in route.dependant.dependencies}
        assert require_authenticated_request in dependencies
        assert route.response_model not in {None, dict}


def test_signal_openapi_has_concrete_unique_read_operations() -> None:
    schema = _app().openapi()
    operation_ids = []
    for path in schema["paths"].values():
        operation = path.get("get")
        if operation is None:
            continue
        operation_ids.append(operation["operationId"])
        response = operation["responses"]["200"]
        assert "$ref" in response["content"]["application/json"]["schema"]
    assert len(operation_ids) == len(set(operation_ids)) == 6


def test_signal_lists_are_paginated_and_forward_the_requested_window() -> None:
    application = _application()
    client = TestClient(_app(application))

    states = client.get("/api/v1/signals/states?page=2&page_size=20")
    events = client.get("/api/v1/signal-events?page=3&page_size=10")
    evaluations = client.get("/api/v1/signal-evaluations?page=4&page_size=5")

    assert states.status_code == events.status_code == evaluations.status_code == 200
    assert states.json()["data"]["pagination"] == {
        "page": 2,
        "page_size": 20,
        "total": 41,
    }
    assert events.json()["data"]["pagination"] == {
        "page": 3,
        "page_size": 10,
        "total": 21,
    }
    assert evaluations.json()["data"]["pagination"] == {
        "page": 4,
        "page_size": 5,
        "total": 16,
    }
    application.list_states.assert_awaited_once_with(page=2, page_size=20)
    application.list_events.assert_awaited_once_with(page=3, page_size=10)
    application.list_evaluations.assert_awaited_once_with(page=4, page_size=5)


def test_signal_detail_routes_return_concrete_views() -> None:
    application = _application()
    client = TestClient(_app(application))

    state = client.get(f"/api/v1/signals/states/{SUBSCRIPTION_ID}")
    event = client.get(f"/api/v1/signal-events/{EVENT_ID}")
    evaluation = client.get(f"/api/v1/signal-evaluations/{EVALUATION_ID}")

    assert state.status_code == event.status_code == evaluation.status_code == 200
    assert state.json()["data"]["subscription_id"] == str(SUBSCRIPTION_ID)
    assert event.json()["data"]["id"] == str(EVENT_ID)
    assert evaluation.json()["data"]["id"] == str(EVALUATION_ID)
    application.get_state.assert_awaited_once_with(SUBSCRIPTION_ID)
    application.get_event.assert_awaited_once_with(EVENT_ID)
    application.get_evaluation.assert_awaited_once_with(EVALUATION_ID)


def test_signal_missing_details_use_stable_error_codes() -> None:
    application = _application()
    application.get_state.return_value = None
    application.get_event.return_value = None
    application.get_evaluation.return_value = None
    client = TestClient(_app(application))

    cases = (
        (f"/api/v1/signals/states/{SUBSCRIPTION_ID}", "SIGNAL_STATE_NOT_FOUND"),
        (f"/api/v1/signal-events/{EVENT_ID}", "SIGNAL_EVENT_NOT_FOUND"),
        (
            f"/api/v1/signal-evaluations/{EVALUATION_ID}",
            "SIGNAL_EVALUATION_NOT_FOUND",
        ),
    )
    for path, code in cases:
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["code"] == code


def test_signal_reads_require_authentication_and_validate_pagination() -> None:
    client = TestClient(_app(authenticated=False))
    unauthorized = client.get("/api/v1/signals/states")
    invalid = TestClient(_app()).get("/api/v1/signal-events?page=0&page_size=201")

    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "AUTH_REQUIRED"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"


def test_signal_reset_and_reevaluate_write_contract_is_exposed() -> None:
    routes = {
        (method, route.path): route
        for route in router.routes
        for method in route.methods
        if method == "POST"
    }
    expected = {
        ("POST", "/api/v1/signals/states/{subscription_id}/reset"),
        ("POST", "/api/v1/signals/states/{subscription_id}/reevaluate"),
    }
    assert set(routes) == expected
    for route in routes.values():
        dependencies = {item.call for item in route.dependant.dependencies}
        assert require_verified_write_request in dependencies
        assert "idempotency_key" in {
            item.name for item in route.dependant.header_params
        }
        assert route.response_model not in {None, dict}
