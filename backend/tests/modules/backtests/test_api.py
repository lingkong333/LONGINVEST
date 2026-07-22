from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from long_invest.modules.backtests.api import (
    CreateBacktestBody,
    create_backtest,
    get_backtest_item,
    router,
)
from long_invest.modules.backtests.contracts import (
    BacktestItemStatus,
    BacktestResultView,
)


class TaskValue:
    def model_dump(self, **_kwargs):
        return {"id": "task-1", "mode": "SINGLE"}


class Application:
    def __init__(self) -> None:
        self.created = None

    async def create(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(
            task=TaskValue(),
            item_id=uuid4(),
            item_status=BacktestItemStatus.PENDING,
            forecast=None,
        )

    async def get_result(self, task_id, item_id):
        return BacktestResultView(
            task_id=task_id,
            item_id=item_id,
            item_status=BacktestItemStatus.PENDING,
            forecast=None,
            adjustments=(),
            orders=(),
            trades=(),
            daily_results=(),
            metric=None,
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


def test_router_exposes_current_single_backtest_read_surface() -> None:
    paths = {route.path for route in router.routes}
    assert {
        "/api/v1/backtests",
        "/api/v1/backtests/{task_id}",
        "/api/v1/backtests/{task_id}/items/{item_id}",
        "/api/v1/backtests/{task_id}/items/{item_id}/target-adjustments",
        "/api/v1/backtests/{task_id}/items/{item_id}/orders",
        "/api/v1/backtests/{task_id}/items/{item_id}/trades",
        "/api/v1/backtests/{task_id}/items/{item_id}/daily-results",
        "/api/v1/backtests/{task_id}/items/{item_id}/metric",
    } <= paths
