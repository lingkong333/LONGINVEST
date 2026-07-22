from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.modules.backtests.contracts import (
    BacktestDateRange,
    BacktestMode,
    BacktestTaskSnapshot,
    BacktestUniverseEntry,
)
from long_invest.modules.backtests.service import (
    BacktestCommandContext,
    BacktestService,
)
from long_invest.modules.strategies.contracts import (
    StrategyForecastResult,
    TrainingDataSnapshot,
)
from long_invest.modules.targets.contracts import TargetValues
from long_invest.platform.errors import AppError


class Repository:
    def __init__(self) -> None:
        self.tasks = {}
        self.items = {}
        self.universes = {}
        self.forecasts = {}

    async def get_task(self, task_id, **_kwargs):
        return self.tasks.get(task_id)

    async def get_task_by_idempotency(self, key, **_kwargs):
        return next(
            (row for row in self.tasks.values() if row.idempotency_key == key), None
        )

    async def get_item(self, task_id, **_kwargs):
        return self.items.get(task_id)

    async def get_universe(self, task_id):
        return self.universes.get(task_id)

    async def get_forecast(self, item_id):
        return self.forecasts.get(item_id)

    async def get_metric(self, _item_id):
        return None

    async def add_task(self, task, universe, item):
        self.tasks[task.id] = task
        self.items[task.id] = item
        self.universes[task.id] = universe

    async def add_forecast(self, forecast):
        if forecast.id is None:
            forecast.id = uuid4()
        self.forecasts[forecast.item_id] = forecast


def test_create_replays_same_idempotency_and_rejects_changed_content() -> None:
    async def scenario() -> None:
        service = BacktestService(Repository())
        first_snapshot = _task()
        context = _context("same-key")
        first = await service.create(first_snapshot, context)
        replay = await service.create(
            first_snapshot.model_copy(update={"id": uuid4()}), context
        )
        assert replay.task.id == first.task.id

        changed = first_snapshot.model_copy(
            update={"id": uuid4(), "initial_capital": Decimal("200000")}
        )
        with pytest.raises(AppError) as captured:
            await service.create(changed, context)
        assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"

    asyncio.run(scenario())


def test_concurrent_loser_cannot_fail_the_active_execution() -> None:
    async def scenario() -> None:
        repository = Repository()
        service = BacktestService(repository)
        snapshot = _task()
        await service.create(snapshot, _context("concurrent"))
        winner = uuid4()
        loser = uuid4()
        await service.start(snapshot.id, winner)

        with pytest.raises(AppError) as captured:
            await service.start(snapshot.id, loser)
        assert captured.value.code == "BACKTEST_ALREADY_RUNNING"
        await service.fail(snapshot.id, "LOSER_FAILURE", execution_token=loser)
        assert repository.items[snapshot.id].status == "FETCHING_DATA"
        assert repository.items[snapshot.id].failure_code is None

    asyncio.run(scenario())


def test_recovery_reuses_first_frozen_test_snapshot() -> None:
    async def scenario() -> None:
        repository = Repository()
        service = BacktestService(repository)
        snapshot = _task()
        await service.create(snapshot, _context("recover"))
        first_token = uuid4()
        await service.start(snapshot.id, first_token)
        training = _data(snapshot, training=True, content="c" * 64)
        await service.claim_forecast(
            snapshot.id, training, execution_token=first_token
        )
        await service.freeze_forecast(
            snapshot.id,
            training,
            StrategyForecastResult(values=_targets()),
            execution_token=first_token,
            frozen_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
        test_data = _data(snapshot, training=False, content="e" * 64)
        await service.claim_simulation(
            snapshot.id, test_data, execution_token=first_token
        )
        await service.fail(
            snapshot.id, "BACKTEST_RESULT_SAVE_FAILED", execution_token=first_token
        )

        recovery_token = uuid4()
        await service.recover(snapshot.id, execution_token=recovery_token)
        await service.claim_simulation(
            snapshot.id, test_data, execution_token=recovery_token
        )
        changed = test_data.model_copy(update={"content_hash": "f" * 64})
        with pytest.raises(AppError) as captured:
            await service.claim_simulation(
                snapshot.id, changed, execution_token=recovery_token
            )
        assert captured.value.code == "TEST_DATA_INVALID"
        assert repository.items[snapshot.id].test_data_hash == "e" * 64

    asyncio.run(scenario())


def _context(key: str) -> BacktestCommandContext:
    return BacktestCommandContext(
        request_id="req-1",
        idempotency_key=key,
        actor_user_id="user-1",
        reason="test",
    )


def _task() -> BacktestTaskSnapshot:
    return BacktestTaskSnapshot(
        id=uuid4(),
        mode=BacktestMode.SINGLE,
        universe_snapshot=(
            BacktestUniverseEntry(
                security_id=uuid4(), symbol="600000.SH", name="浦发银行"
            ),
        ),
        universe_hash="f" * 64,
        date_range=BacktestDateRange(
            training_start_date=date(2024, 1, 1),
            training_end_date=date(2024, 12, 31),
            test_start_date=date(2025, 1, 1),
            test_end_date=date(2025, 12, 31),
        ),
        strategy_version_id=uuid4(),
        draft_id=None,
        draft_version=None,
        draft_source_code=None,
        source_code_hash="a" * 64,
        strategy_metadata={},
        parameter_schema={},
        parameter_snapshot={},
        parameter_hash="b" * 64,
        environment_version="python-3.12",
        runner_image_digest="sha256:" + "d" * 64,
        strategy_api_version="1.0",
        rule_version="signals-price-zone-v1",
        hysteresis_ratio="0.02",
        minimum_hysteresis="0.02",
        initial_capital="100000",
        price_basis="QFQ_AS_OF",
        data_source="EASTMONEY",
    )


def _data(task: BacktestTaskSnapshot, *, training: bool, content: str):
    start = (
        task.date_range.training_start_date
        if training
        else task.date_range.test_start_date
    )
    end = (
        task.date_range.training_end_date if training else task.date_range.test_end_date
    )
    entry = task.universe_snapshot[0]
    return TrainingDataSnapshot(
        security_id=entry.security_id,
        symbol=entry.symbol,
        start_date=start,
        end_date=end,
        data_version=1,
        fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
        source=task.data_source,
        price_basis=task.price_basis,
        content_hash=content,
        rows=(
            {
                "trade_date": end,
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
            },
        ),
    )


def _targets() -> TargetValues:
    return TargetValues(
        low_strong="8", low_watch="9", high_watch="11", high_strong="12"
    )
