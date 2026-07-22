from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.modules.backtests.contracts import (
    BacktestAction,
    BacktestDateRange,
    BacktestMode,
    BacktestTaskSnapshot,
    BacktestUniverseEntry,
)
from long_invest.modules.backtests.service import (
    BacktestCommandContext,
    BacktestService,
)
from long_invest.modules.market_data.contracts import AdjustmentTimelineSnapshot
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
        self.adjustment_snapshots = {}
        self.controls = {}

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

    async def get_adjustment_snapshot(self, item_id):
        return self.adjustment_snapshots.get(item_id)

    async def get_metric(self, _item_id):
        return None

    async def get_control_by_idempotency(self, key, **_kwargs):
        return self.controls.get(key)

    async def add_control(self, command):
        self.controls[command.idempotency_key] = command

    async def list_tasks(self, *, page, page_size):
        rows = sorted(
            self.tasks.values(), key=lambda row: (row.created_at, row.id), reverse=True
        )
        start = (page - 1) * page_size
        values = []
        for task in rows[start : start + page_size]:
            item = self.items[task.id]
            universe = self.universes[task.id]
            values.append((task, item, universe, item.id in self.forecasts))
        return values, len(rows)

    async def list_items(self, task_id):
        item = self.items.get(task_id)
        return [item] if item is not None else []

    async def add_task(self, task, universe, item):
        self.tasks[task.id] = task
        self.items[task.id] = item
        self.universes[task.id] = universe

    async def add_forecast(self, forecast):
        if forecast.id is None:
            forecast.id = uuid4()
        self.forecasts[forecast.item_id] = forecast

    async def add_adjustment_snapshot(self, snapshot):
        if snapshot.id is None:
            snapshot.id = uuid4()
        self.adjustment_snapshots[snapshot.item_id] = snapshot


class EventSink:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


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
        await service.start(snapshot.id, winner, expected_generation=1)

        with pytest.raises(AppError) as captured:
            await service.start(snapshot.id, loser, expected_generation=1)
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
        await service.start(snapshot.id, first_token, expected_generation=1)
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
        await service.retry_failed(snapshot.id, _context("recover-retry"))
        await service.recover(
            snapshot.id, execution_token=recovery_token, expected_generation=2
        )
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


def test_adjustment_snapshot_freezes_empty_coverage_for_recovery() -> None:
    async def scenario() -> None:
        repository = Repository()
        service = BacktestService(repository)
        snapshot = _task()
        token = uuid4()
        await service.create(snapshot, _context("adjustments"))
        await service.start(snapshot.id, token, expected_generation=1)
        training = _data(snapshot, training=True, content="c" * 64)
        await service.claim_forecast(snapshot.id, training, execution_token=token)
        await service.freeze_forecast(
            snapshot.id,
            training,
            StrategyForecastResult(values=_targets()),
            execution_token=token,
            frozen_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
        test_data = _data(snapshot, training=False, content="e" * 64)
        await service.claim_simulation(snapshot.id, test_data, execution_token=token)
        timeline = AdjustmentTimelineSnapshot(
            snapshot_id=uuid4(),
            security_id=snapshot.universe_snapshot[0].security_id,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            as_of=datetime(2026, 7, 21, tzinfo=UTC),
            source="EASTMONEY",
            provider_contract_version="corporate-actions-v1",
            fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
            row_count=0,
            content_hash="f" * 64,
            entries=(),
        )

        frozen = await service.freeze_adjustment_snapshot(
            snapshot.id, timeline, execution_token=token
        )
        assert frozen == timeline
        execution = await service.get_execution(snapshot.id)
        assert execution.adjustment_snapshot == timeline

    asyncio.run(scenario())


def test_pause_resume_checkpoint_and_old_generation_fencing() -> None:
    async def scenario() -> None:
        repository = Repository()
        service = BacktestService(repository)
        snapshot = _task()
        await service.create(snapshot, _context("create-control"))
        token = uuid4()
        await service.start(snapshot.id, token, expected_generation=1)

        pausing = await service.pause(snapshot.id, _context("pause-1"))
        assert pausing.task_status.value == "PAUSING"
        paused = await service.checkpoint(
            snapshot.id, token, expected_generation=1
        )
        assert paused.task_status.value == "PAUSED"
        assert repository.items[snapshot.id].execution_token is None

        resumed = await service.resume(snapshot.id, _context("resume-1"))
        assert resumed.task_status.value == "PENDING"
        assert resumed.execution_generation == 2
        with pytest.raises(AppError) as captured:
            await service.start(snapshot.id, uuid4(), expected_generation=1)
        assert captured.value.code == "BACKTEST_EXECUTION_SUPERSEDED"

    asyncio.run(scenario())


def test_cancel_running_task_finishes_at_checkpoint() -> None:
    async def scenario() -> None:
        repository = Repository()
        service = BacktestService(repository)
        snapshot = _task()
        await service.create(snapshot, _context("create-cancel"))
        token = uuid4()
        await service.start(snapshot.id, token, expected_generation=1)

        canceling = await service.cancel(snapshot.id, _context("cancel-1"))
        assert canceling.task_status.value == "CANCELING"
        canceled = await service.checkpoint(
            snapshot.id, token, expected_generation=1
        )
        assert canceled.task_status.value == "CANCELED"
        assert canceled.item_status.value == "CANCELED"
        assert repository.tasks[snapshot.id].terminal_at is not None

    asyncio.run(scenario())


def test_pause_racing_with_result_save_emits_paused_event() -> None:
    async def scenario() -> None:
        repository = Repository()
        events = EventSink()
        service = BacktestService(repository, events=events)
        snapshot = _task()
        token = uuid4()
        await service.create(snapshot, _context("create-save-race"))
        await service.start(snapshot.id, token, expected_generation=1)
        await service.pause(snapshot.id, _context("pause-save-race"))

        state = await service.save_success(
            snapshot.id,
            _data(snapshot, training=False, content="e" * 64),
            None,
            execution_token=token,
        )

        assert state.task_status.value == "PAUSED"
        assert events.events[-1].topic == "backtest.paused"

    asyncio.run(scenario())


def test_retry_failed_reuses_forecast_and_rejects_unknown_forecast_result() -> None:
    async def scenario() -> None:
        repository = Repository()
        service = BacktestService(repository)
        snapshot = _task()
        await service.create(snapshot, _context("create-retry"))
        token = uuid4()
        await service.start(snapshot.id, token, expected_generation=1)
        training = _data(snapshot, training=True, content="c" * 64)
        await service.claim_forecast(snapshot.id, training, execution_token=token)
        await service.fail(snapshot.id, "FORECAST_UNKNOWN", execution_token=token)

        with pytest.raises(AppError) as captured:
            await service.retry_failed(snapshot.id, _context("retry-unknown"))
        assert captured.value.code == "BACKTEST_NOT_RECOVERABLE"

        repository.items[snapshot.id].status = "FORECASTING"
        repository.items[snapshot.id].failure_code = None
        repository.items[snapshot.id].execution_token = token
        await service.freeze_forecast(
            snapshot.id,
            training,
            StrategyForecastResult(values=_targets()),
            execution_token=token,
            frozen_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
        await service.fail(snapshot.id, "SAVE_FAILED", execution_token=token)
        retried = await service.retry_failed(snapshot.id, _context("retry-safe"))
        assert retried.item_status.value == "FROZEN"
        assert retried.execution_generation == 2

    asyncio.run(scenario())


def test_control_idempotency_rerun_and_summary() -> None:
    async def scenario() -> None:
        repository = Repository()
        service = BacktestService(repository)
        snapshot = _task()
        await service.create(snapshot, _context("create-rerun"))
        first = await service.pause(snapshot.id, _context("same-pause"))
        replay = await service.pause(snapshot.id, _context("same-pause"))
        assert replay.task_status == first.task_status

        with pytest.raises(AppError) as captured:
            await service.cancel(snapshot.id, _context("same-pause"))
        assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"

        await service.cancel(snapshot.id, _context("cancel-paused"))
        rerun_id = uuid4()
        rerun = await service.rerun(
            snapshot.id, rerun_id, _context("rerun-1")
        )
        assert rerun.task.id == rerun_id
        assert repository.tasks[rerun_id].rerun_from_task_id == snapshot.id
        assert repository.items[rerun_id].attempt_count == 0

        page = await service.list_tasks(page=1, page_size=10)
        assert page.total == 2
        assert page.items[0].task_id == rerun_id
        assert BacktestAction.PAUSE in page.items[0].allowed_actions
        summary = await service.get_summary(snapshot.id)
        assert summary.canceled_items == 1
        assert summary.completed_items == 1
        assert summary.allowed_actions == (BacktestAction.RERUN,)

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
