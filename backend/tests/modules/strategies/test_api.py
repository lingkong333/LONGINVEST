from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from long_invest.modules.auth.dependencies import require_verified_write_request
from long_invest.modules.strategies.api import router
from long_invest.modules.strategies.application import get_strategy_application
from long_invest.platform.errors import AppError


class Application:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        strategy_id = uuid4()
        return SimpleNamespace(
            strategy=SimpleNamespace(
                id=strategy_id, name=kwargs["name"], status="DRAFT"
            ),
            draft=SimpleNamespace(
                id=uuid4(),
                strategy_id=strategy_id,
                draft_version=1,
                source_code="",
                strategy_metadata={},
                parameter_schema={},
            ),
        )

    async def publish(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            run=SimpleNamespace(id=uuid4(), status="PENDING"),
            version=SimpleNamespace(id=uuid4()),
        )

    async def request_validation(self, strategy_id, **kwargs):
        self.calls.append({"strategy_id": strategy_id, **kwargs})
        return SimpleNamespace(
            id=uuid4(),
            strategy_id=strategy_id,
            strategy_version_id=None,
            draft_version=1,
            source_code_hash="a" * 64,
            status="PENDING",
            error_code=None,
            created_at=datetime(2026, 7, 22, tzinfo=UTC),
            completed_at=None,
        )

    async def restore(self, strategy_id, **kwargs):
        self.calls.append({"strategy_id": strategy_id, **kwargs})
        return SimpleNamespace(id=strategy_id, name="策略", status="PUBLISHED")


def client(application):
    app = FastAPI()

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    app.include_router(router)
    app.dependency_overrides[get_strategy_application] = lambda: application
    app.dependency_overrides[require_verified_write_request] = lambda: SimpleNamespace(
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=uuid4()),
        audit_context=SimpleNamespace(request_id="req-1", trusted_ip="127.0.0.1"),
    )
    return TestClient(app)


def test_create_requires_explicit_confirmation():
    response = client(Application()).post(
        "/api/v1/strategies",
        headers={"Idempotency-Key": "create-1"},
        json={"name": "策略", "confirm": False, "reason": "创建策略"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "AUTH_CONFIRMATION_REQUIRED"


def test_create_passes_verified_identity_reason_and_idempotency():
    application = Application()
    response = client(application).post(
        "/api/v1/strategies",
        headers={"Idempotency-Key": "create-1"},
        json={"name": "策略", "confirm": True, "reason": "创建策略"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["draft"]["draft_version"] == 1
    assert "SAVE_DRAFT" in response.json()["data"]["strategy"]["allowed_actions"]
    assert application.calls[0]["idempotency_key"] == "create-1"
    assert application.calls[0]["reason"] == "创建策略"


def test_every_strategy_write_route_uses_verified_write_dependency():
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}
    write_routes = [
        route
        for route in router.routes
        if route.methods and route.methods.intersection(write_methods)
    ]

    assert write_routes
    for route in write_routes:
        calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_verified_write_request in calls, route.path


def test_strategy_operation_routes_are_published():
    paths = {
        (route.path, method)
        for route in router.routes
        for method in (route.methods or set())
    }
    assert {
        ("/api/v1/strategies/{strategy_id}/test", "POST"),
        (
            "/api/v1/strategies/{strategy_id}/versions/{version_id}/apply",
            "POST",
        ),
        (
            "/api/v1/strategies/{strategy_id}/versions/{version_id}/rollback",
            "POST",
        ),
    } <= paths


def test_publish_request_cannot_replace_validated_metadata_or_schema():
    strategy_id = uuid4()
    response = client(Application()).post(
        f"/api/v1/strategies/{strategy_id}/publish",
        headers={"Idempotency-Key": "publish-1"},
        json={
            "validation_run_id": str(uuid4()),
            "expected_draft_version": 1,
            "metadata": {"name": "替换内容"},
            "parameter_schema": {"type": "string"},
            "confirm": True,
            "reason": "发布",
        },
    )

    assert response.status_code == 422


def test_publish_returns_accepted_run_without_executing_work_inline():
    application = Application()
    strategy_id = uuid4()

    response = client(application).post(
        f"/api/v1/strategies/{strategy_id}/publish",
        headers={"Idempotency-Key": "publish-1"},
        json={
            "validation_run_id": str(uuid4()),
            "expected_draft_version": 1,
            "confirm": True,
            "reason": "发布",
        },
    )

    assert response.status_code == 202
    assert response.json()["code"] == "STRATEGY_PUBLISH_REQUESTED"
    assert response.json()["data"]["status"] == "PENDING"


def test_validation_requires_an_explicit_backtest_task():
    strategy_id = uuid4()
    response = client(Application()).post(
        f"/api/v1/strategies/{strategy_id}/validate",
        headers={"Idempotency-Key": "validate-1"},
        json={
            "metadata": {"name": "策略"},
            "parameter_schema": {"type": "object"},
            "params": {},
            "confirm": True,
            "reason": "验证",
        },
    )

    assert response.status_code == 422


def test_validation_passes_the_selected_backtest_task_unchanged():
    application = Application()
    strategy_id = uuid4()
    backtest_task_id = uuid4()
    response = client(application).post(
        f"/api/v1/strategies/{strategy_id}/validate",
        headers={"Idempotency-Key": "validate-1"},
        json={
            "backtest_task_id": str(backtest_task_id),
            "params": {"window": 20},
            "confirm": True,
            "reason": "验证",
        },
    )

    assert response.status_code == 202
    assert application.calls[0]["backtest_task_id"] == backtest_task_id


def test_archived_strategy_has_explicit_restore_api():
    application = Application()
    strategy_id = uuid4()

    response = client(application).post(
        f"/api/v1/strategies/{strategy_id}/restore",
        headers={"Idempotency-Key": "restore-1"},
        json={"expected_version": 3, "confirm": True, "reason": "恢复"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "PUBLISHED"
