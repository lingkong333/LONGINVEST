from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from long_invest.modules.backtests.api import (
    BacktestCommandBody,
    CreateBacktestBody,
    create_backtest,
    get_backtest_item,
    pause_backtest,
    router,
)
from long_invest.modules.backtests.contracts import (
    BacktestAction,
    BacktestItemStatus,
    BacktestResultView,
    BacktestTaskStatus,
)


class TaskValue:
    def model_dump(self, **_kwargs):
        return {"id": "task-1", "mode": "SINGLE"}


class Application:
    def __init__(self) -> None:
        self.created = None
        self.paused = None

    async def create(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(
            task=TaskValue(),
            item_id=uuid4(),
            item_status=BacktestItemStatus.PENDING,
            forecast=None,
            test_data_snapshot=None,
        )

    async def get_result(self, task_id, item_id):
        return BacktestResultView(
            task_id=task_id,
            item_id=item_id,
            item_status=BacktestItemStatus.PENDING,
            forecast=None,
            test_data_snapshot=None,
            adjustments=(),
            orders=(),
            trades=(),
            daily_results=(),
            metric=None,
        )

    async def pause(self, task_id, context):
        self.paused = (task_id, context)

    async def get_summary(self, task_id):
        return SimpleNamespace(
            task_id=task_id,
            status=BacktestTaskStatus.PAUSED,
            allowed_actions=(BacktestAction.RESUME, BacktestAction.CANCEL),
        )


def test_create_api_uses_stable_task_id_and_command_context() -> None:
    async def scenario() -> None:
        application = Application()
        identity = SimpleNamespace(
            audit_context=SimpleNamespace(request_id="req-1"),
            user=SimpleNamespace(id=uuid4()),
        )
        body = CreateBacktestBody(
            symbol="600000.SH",
            date_range={
                "training_start_date": date(2024, 1, 1),
                "training_end_date": date(2024, 12, 31),
                "test_start_date": date(2025, 1, 1),
                "test_end_date": date(2025, 12, 31),
            },
            strategy_version_id=uuid4(),
            parameter_snapshot={},
            initial_capital=Decimal("100000"),
            confirm=True,
            reason="验证样本外效果",
        )

        response = await create_backtest(body, application, identity, "key-1")

        assert response["code"] == "BACKTEST_CREATED"
        assert application.created["context"].idempotency_key == "key-1"
        first_id = application.created["task_id"]
        await create_backtest(body, application, identity, "key-1")
        assert application.created["task_id"] == first_id

    asyncio.run(scenario())


def test_item_api_returns_empty_in_progress_result() -> None:
    async def scenario() -> None:
        task_id = uuid4()
        item_id = uuid4()
        response = await get_backtest_item(
            task_id, item_id, Application(), SimpleNamespace()
        )
        assert response["data"]["task_id"] == str(task_id)
        assert response["data"]["item_status"] == "PENDING"
        assert response["data"]["orders"] == []

    asyncio.run(scenario())


def test_pause_api_requires_command_context_and_returns_allowed_actions() -> None:
    async def scenario() -> None:
        application = Application()
        task_id = uuid4()
        identity = SimpleNamespace(
            audit_context=SimpleNamespace(request_id="req-2", trusted_ip="127.0.0.1"),
            user=SimpleNamespace(id=uuid4()),
            session=SimpleNamespace(id=uuid4()),
        )

        response = await pause_backtest(
            task_id,
            BacktestCommandBody(confirm=True, reason=" 暂停检查 "),
            application,
            identity,
            "pause-key",
        )

        assert response["code"] == "JOB_ACCEPTED"
        assert response["data"] == {
            "task_id": str(task_id),
            "status": "PAUSED",
            "allowed_actions": ["RESUME", "CANCEL"],
        }
        assert application.paused[1].reason == "暂停检查"
        assert application.paused[1].session_id == str(identity.session.id)

    asyncio.run(scenario())


def test_router_exposes_current_single_backtest_read_surface() -> None:
    paths = {route.path for route in router.routes}
    assert {
        "/api/v1/backtests",
        "/api/v1/backtests/{task_id}",
        "/api/v1/backtests/{task_id}/summary",
        "/api/v1/backtests/{task_id}/items",
        "/api/v1/backtests/{task_id}/pause",
        "/api/v1/backtests/{task_id}/resume",
        "/api/v1/backtests/{task_id}/cancel",
        "/api/v1/backtests/{task_id}/retry-failed",
        "/api/v1/backtests/{task_id}/rerun",
        "/api/v1/backtests/{task_id}/items/{item_id}",
        "/api/v1/backtests/{task_id}/items/{item_id}/target-adjustments",
        "/api/v1/backtests/{task_id}/items/{item_id}/orders",
        "/api/v1/backtests/{task_id}/items/{item_id}/trades",
        "/api/v1/backtests/{task_id}/items/{item_id}/daily-results",
        "/api/v1/backtests/{task_id}/items/{item_id}/metric",
    } <= paths


def test_control_and_summary_openapi_publish_concrete_responses() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    paths = app.openapi()["paths"]
    operations = (
        ("/api/v1/backtests", "get", "200"),
        ("/api/v1/backtests/{task_id}/summary", "get", "200"),
        ("/api/v1/backtests/{task_id}/items", "get", "200"),
        ("/api/v1/backtests/{task_id}/pause", "post", "202"),
        ("/api/v1/backtests/{task_id}/resume", "post", "202"),
        ("/api/v1/backtests/{task_id}/cancel", "post", "202"),
        ("/api/v1/backtests/{task_id}/retry-failed", "post", "202"),
        ("/api/v1/backtests/{task_id}/rerun", "post", "202"),
    )
    for path, method, status in operations:
        schema = paths[path][method]["responses"][status]["content"][
            "application/json"
        ]["schema"]
        assert "$ref" in schema

    pause = paths["/api/v1/backtests/{task_id}/pause"]["post"]
    header = next(
        parameter
        for parameter in pause["parameters"]
        if parameter["in"] == "header"
        and parameter["name"] == "Idempotency-Key"
    )
    assert header["required"] is True
