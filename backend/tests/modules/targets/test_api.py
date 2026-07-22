from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from long_invest.modules.auth.application import get_auth_application
from long_invest.modules.auth.dependencies import (
    AUTH_COOKIE_NAME,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.targets.api import (
    CalculateTargetRequest,
    CapabilityWriteRequest,
    ManualTargetRequest,
    RestoreTargetRequest,
    get_target_application,
    router,
)
from long_invest.modules.targets.application import (
    CalculationSubmission,
    TargetApplication,
)
from long_invest.modules.targets.contracts import (
    TargetBindingView,
    TargetMutationResult,
    TargetRevisionView,
    TargetSnapshot,
    TargetSource,
    TargetStatus,
    TargetValues,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import (
    app_error_handler,
    register_exception_handlers,
    validation_error_handler,
)
from long_invest.platform.http.middleware import RequestContextMiddleware

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)
SUBSCRIPTION_ID = uuid4()
REVISION_ID = uuid4()


def _identity():
    return SimpleNamespace(
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=uuid4()),
        audit_context=SimpleNamespace(request_id="req-api", trusted_ip="127.0.0.1"),
    )


def _snapshot():
    return TargetSnapshot(
        subscription_id=SUBSCRIPTION_ID,
        revision_id=REVISION_ID,
        revision_no=1,
        binding_version=2,
        values=TargetValues(
            low_strong="8", low_watch="9", high_watch="12", high_strong="13"
        ),
        source=TargetSource.MANUAL,
        status=TargetStatus.READY,
        target_date=date(2026, 7, 17),
        parameter_snapshot={},
        content_hash="a" * 64,
        activated_at=NOW,
    )


def _revision():
    snapshot = _snapshot()
    return TargetRevisionView(
        id=snapshot.revision_id,
        subscription_id=snapshot.subscription_id,
        revision_no=snapshot.revision_no,
        values=snapshot.values,
        source=snapshot.source,
        target_date=snapshot.target_date,
        parameter_snapshot={},
        content_hash=snapshot.content_hash,
        reason="manual",
        created_at=NOW,
    )


def _mutation():
    return TargetMutationResult(
        code="TARGET_MANUAL_ACTIVATED",
        binding=TargetBindingView(
            subscription_id=SUBSCRIPTION_ID,
            current_revision_id=REVISION_ID,
            status=TargetStatus.READY,
            version=2,
            activated_at=NOW,
        ),
        revision=_revision(),
    )


def _application():
    application = SimpleNamespace(
        list=AsyncMock(return_value=((_snapshot(),), 1)),
        get=AsyncMock(return_value=_snapshot()),
        history=AsyncMock(return_value=((_revision(),), 1)),
        set_manual=AsyncMock(return_value=_mutation()),
        restore=AsyncMock(return_value=_mutation()),
        calculate=AsyncMock(
            return_value=CalculationSubmission(
                "TARGET_CALCULATION_ACCEPTED", uuid4(), uuid4()
            )
        ),
        recalculate_review=AsyncMock(
            return_value=CalculationSubmission(
                "TARGET_RECALCULATION_ACCEPTED", uuid4(), uuid4()
            )
        ),
        list_calculation_runs=AsyncMock(return_value=((), 0)),
        list_reviews=AsyncMock(return_value=((), 0)),
        decide_review=AsyncMock(),
    )
    return application


def _app(application=None, *, authenticated=True):
    value = FastAPI()
    register_exception_handlers(value)
    value.include_router(router)
    value.dependency_overrides[get_target_application] = lambda: (
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


def _manual_body(**updates):
    body = {
        "confirm": True,
        "target_date": "2026-07-17",
        "values": {
            "low_strong": "8",
            "low_watch": "9",
            "high_watch": "12",
            "high_strong": "13",
        },
        "reason": "manual target",
        "expected_version": 1,
        "large_change_confirmed": True,
        "switch_to_manual_confirmed": True,
    }
    body.update(updates)
    return body


def _restore_body(**updates):
    body = {
        "confirm": True,
        "source_revision_id": str(REVISION_ID),
        "reason": "restore target",
        "expected_version": 2,
        "switch_to_manual_confirmed": True,
    }
    body.update(updates)
    return body


def test_target_router_freezes_routes_auth_headers_and_response_models() -> None:
    routes = {
        (method, route.path): route
        for route in router.routes
        for method in route.methods
    }
    assert set(routes) == {
        ("GET", "/api/v1/targets"),
        ("GET", "/api/v1/targets/{subscription_id}"),
        ("GET", "/api/v1/targets/{subscription_id}/history"),
        ("POST", "/api/v1/targets/{subscription_id}/manual"),
        ("POST", "/api/v1/targets/{subscription_id}/restore"),
        ("POST", "/api/v1/targets/{subscription_id}/calculate"),
        ("POST", "/api/v1/targets/{subscription_id}/retry"),
        ("POST", "/api/v1/targets/calculate-batch"),
        ("GET", "/api/v1/target-calculation-runs"),
        ("GET", "/api/v1/target-reviews"),
        ("POST", "/api/v1/target-reviews/{review_id}/approve"),
        ("POST", "/api/v1/target-reviews/{review_id}/reject"),
        ("POST", "/api/v1/target-reviews/{review_id}/recalculate"),
    }
    for (method, _path), route in routes.items():
        dependencies = {item.call for item in route.dependant.dependencies}
        assert (
            require_authenticated_request
            if method == "GET"
            else require_verified_write_request
        ) in dependencies
        assert route.response_model not in {None, dict}
        if method == "POST":
            assert "idempotency_key" in {
                item.name for item in route.dependant.header_params
            }


def test_target_openapi_has_concrete_unique_operations_and_required_headers() -> None:
    schema = _app().openapi()
    operation_ids = []
    for path in schema["paths"].values():
        for operation in path.values():
            operation_ids.append(operation["operationId"])
            response = operation["responses"].get("200") or operation["responses"].get(
                "202"
            )
            assert "$ref" in response["content"]["application/json"]["schema"]
            if operation in [path.get("post")]:
                header = next(
                    item
                    for item in operation["parameters"]
                    if item["name"] == "Idempotency-Key"
                )
                assert header["required"] is True
                assert header["schema"]["minLength"] == 1
                assert header["schema"]["maxLength"] == 200
    assert len(operation_ids) == len(set(operation_ids))


@pytest.mark.parametrize(
    ("request_type", "body"),
    [
        (ManualTargetRequest, _manual_body(extra="forbidden")),
        (RestoreTargetRequest, _restore_body(extra="forbidden")),
        (
            CapabilityWriteRequest,
            {"confirm": True, "reason": "x", "expected_version": 1, "extra": 1},
        ),
    ],
)
def test_target_write_bodies_are_strict(request_type, body) -> None:
    with pytest.raises(ValidationError):
        request_type.model_validate(body)


def test_authenticated_reads_paginate_and_missing_current_is_404() -> None:
    application = _application()
    client = TestClient(_app(application))

    listed = client.get("/api/v1/targets?page=2&page_size=20")
    current = client.get(f"/api/v1/targets/{SUBSCRIPTION_ID}")
    history = client.get(
        f"/api/v1/targets/{SUBSCRIPTION_ID}/history?page=2&page_size=20"
    )

    assert listed.status_code == current.status_code == history.status_code == 200
    assert listed.json()["data"]["pagination"] == {
        "page": 2,
        "page_size": 20,
        "total": 1,
    }
    application.list.assert_awaited_once_with(page=2, page_size=20)
    assert history.json()["data"]["pagination"] == {
        "page": 2,
        "page_size": 20,
        "total": 1,
    }
    application.history.assert_awaited_once_with(SUBSCRIPTION_ID, page=2, page_size=20)
    application.get.return_value = None
    missing = client.get(f"/api/v1/targets/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "TARGET_REVISION_NOT_FOUND"


def test_manual_and_restore_map_verified_identity_and_confirmation_fields() -> None:
    application = _application()
    client = TestClient(_app(application))
    headers = {"Idempotency-Key": "idem-api"}

    manual = client.post(
        f"/api/v1/targets/{SUBSCRIPTION_ID}/manual",
        json=_manual_body(),
        headers=headers,
    )
    restore = client.post(
        f"/api/v1/targets/{SUBSCRIPTION_ID}/restore",
        json=_restore_body(),
        headers=headers,
    )

    assert manual.status_code == restore.status_code == 200
    manual_command = application.set_manual.await_args.args[0]
    assert manual_command.large_change_confirmed is True
    assert manual_command.switch_to_manual_confirmed is True
    assert manual_command.idempotency_key == "idem-api"
    assert manual_command.request_id == "req-api"
    restore_command = application.restore.await_args.args[0]
    assert restore_command.switch_to_manual_confirmed is True
    assert restore_command.source_revision_id == REVISION_ID


@pytest.mark.parametrize(
    "path,body",
    [
        (f"/api/v1/targets/{SUBSCRIPTION_ID}/manual", _manual_body(confirm=False)),
        (f"/api/v1/targets/{SUBSCRIPTION_ID}/restore", _restore_body(confirm=False)),
    ],
)
def test_write_requires_header_and_explicit_true_confirmation(path, body) -> None:
    client = TestClient(_app())

    assert client.post(path, json=body).status_code == 422
    response = client.post(path, json=body, headers={"Idempotency-Key": "idem"})
    assert response.status_code == 409
    assert response.json()["code"] == "TARGET_CONFIRMATION_REQUIRED"


def _calculate_body():
    return {
        "confirm": True,
        "reason": "calculate",
        "expected_version": 1,
        "target_date": "2026-07-17",
        "training_start_date": "2020-01-01",
        "training_end_date": "2025-12-31",
    }


def test_calculation_capability_is_formally_available() -> None:
    client = TestClient(_app())
    response = client.post(
        f"/api/v1/targets/{SUBSCRIPTION_ID}/calculate",
        json=_calculate_body(),
        headers={"Idempotency-Key": "calculate"},
    )
    read = client.get("/api/v1/target-calculation-runs")

    assert response.status_code == 202
    assert read.status_code == 200
    assert response.json()["code"] == "TARGET_CALCULATION_ACCEPTED"
    assert read.json()["data"]["items"] == []


def test_capability_write_requires_expected_version() -> None:
    with pytest.raises(ValidationError):
        CapabilityWriteRequest.model_validate({"confirm": True, "reason": "calculate"})

    with pytest.raises(ValidationError):
        CalculateTargetRequest.model_validate(
            {"confirm": True, "reason": "calculate", "expected_version": 1}
        )


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (f"/api/v1/targets/{SUBSCRIPTION_ID}/manual", _manual_body()),
        (f"/api/v1/targets/{SUBSCRIPTION_ID}/restore", _restore_body()),
        (
            f"/api/v1/targets/{SUBSCRIPTION_ID}/calculate",
            _calculate_body(),
        ),
    ],
)
def test_blank_idempotency_key_is_stable_422(path, body) -> None:
    response = TestClient(_app()).post(
        path, json=body, headers={"Idempotency-Key": "   "}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_unauthenticated_target_routes_are_rejected() -> None:
    client = TestClient(_app(authenticated=False))

    assert client.get("/api/v1/targets").status_code == 401
    assert (
        client.post(
            f"/api/v1/targets/{SUBSCRIPTION_ID}/manual",
            json=_manual_body(),
            headers={"Idempotency-Key": "idem"},
        ).status_code
        == 401
    )


def test_manual_write_uses_origin_session_and_csrf_protection() -> None:
    application = _application()
    user_id, session_id = uuid4(), uuid4()

    class FakeAuthApplication:
        async def validate_write_request(self, **_kwargs):
            return SimpleNamespace(
                user=SimpleNamespace(id=user_id),
                session=SimpleNamespace(id=session_id),
            )

    value = FastAPI()
    value.add_middleware(RequestContextMiddleware)
    value.add_exception_handler(AppError, app_error_handler)
    value.add_exception_handler(RequestValidationError, validation_error_handler)
    value.include_router(router)
    value.dependency_overrides[get_target_application] = lambda: application
    value.dependency_overrides[get_auth_application] = lambda: FakeAuthApplication()
    client = TestClient(value)
    url = f"/api/v1/targets/{SUBSCRIPTION_ID}/manual"
    headers = {
        "Origin": "http://127.0.0.1:15173",
        "X-CSRF-Token": "csrf-token",
        "Idempotency-Key": "auth-target",
        "Cookie": f"{AUTH_COOKIE_NAME}=session-token",
    }

    no_session = client.post(
        url,
        json=_manual_body(),
        headers={key: value for key, value in headers.items() if key != "Cookie"},
    )
    no_origin = client.post(
        url,
        json=_manual_body(),
        headers={key: value for key, value in headers.items() if key != "Origin"},
    )
    no_csrf = client.post(
        url,
        json=_manual_body(),
        headers={key: value for key, value in headers.items() if key != "X-CSRF-Token"},
    )
    accepted = client.post(url, json=_manual_body(), headers=headers)

    assert no_session.status_code == 401
    assert no_origin.status_code == 403
    assert no_csrf.status_code == 403
    assert accepted.status_code == 200
    command = application.set_manual.await_args.args[0]
    assert command.actor_user_id == str(user_id)
    assert command.session_id == str(session_id)


def test_authentication_runs_before_default_application_placeholder() -> None:
    unauthenticated = FastAPI()
    register_exception_handlers(unauthenticated)
    unauthenticated.include_router(router)

    async def reject():
        raise AppError(code="AUTH_REQUIRED", message="login", status_code=401)

    unauthenticated.dependency_overrides[require_authenticated_request] = reject
    unauthenticated.dependency_overrides[require_verified_write_request] = reject
    client = TestClient(unauthenticated)
    assert client.get("/api/v1/targets").status_code == 401
    assert (
        client.post(
            f"/api/v1/targets/{SUBSCRIPTION_ID}/manual",
            json=_manual_body(),
            headers={"Idempotency-Key": "auth-first"},
        ).status_code
        == 401
    )


def test_default_target_application_is_production_ready() -> None:
    application = get_target_application()

    assert isinstance(application, TargetApplication)
    assert callable(application._subscription_factory)
