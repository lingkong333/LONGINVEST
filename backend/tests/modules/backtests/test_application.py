from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from long_invest.modules.backtests.application import (
    BacktestApplication,
    build_backtest_job_handler,
)
from long_invest.modules.backtests.contracts import (
    BacktestDateRange,
    BacktestForecastSnapshotView,
    BacktestItemStatus,
    BacktestMode,
    BacktestTaskSnapshot,
    BacktestUniverseEntry,
)
from long_invest.modules.backtests.engine import FixedTargetBacktestEngine
from long_invest.modules.backtests.service import BacktestExecutionState
from long_invest.modules.backtests.signal_rule import BacktestProductionSignalRule
from long_invest.modules.signals.rules import ProductionPriceZoneRule
from long_invest.modules.strategies.contracts import (
    StrategyForecastResult,
    TrainingDataSnapshot,
)
from long_invest.modules.targets.contracts import TargetValues
from long_invest.platform.jobs.contracts import JobExecutionContext


class FakeDatabase:
    @asynccontextmanager
    async def session(self):
        yield object()

    @asynccontextmanager
    async def transaction(self):
        yield object()


class FakeService:
    def __init__(self, task: BacktestTaskSnapshot) -> None:
        self.task = task
        self.item_id = uuid4()
        self.forecast = None
        self.calls: list[str] = []
        self.failure_code = None
        self.saved_result = None

    async def start(self, _task_id, _execution_token):
        self.calls.append("start")
        return BacktestExecutionState(
            task=self.task,
            item_id=self.item_id,
            item_status="FETCHING_DATA",
            forecast=self.forecast,
        )

    async def claim_forecast(self, _task_id, _training, **_kwargs):
        self.calls.append("claim_forecast")
        return BacktestExecutionState(
            task=self.task,
            item_id=self.item_id,
            item_status="FORECASTING",
            forecast=self.forecast,
        )

    async def freeze_forecast(
        self, _task_id, training, result, *, frozen_at, **_kwargs
    ):
        self.calls.append("freeze_forecast")
        self.forecast = BacktestForecastSnapshotView(
            item_id=self.item_id,
            training_start_date=training.start_date,
            training_end_date=training.end_date,
            training_row_count=len(training.rows),
            training_fetched_at=training.fetched_at,
            training_data_hash=training.content_hash,
            source_code_hash=self.task.source_code_hash,
            parameter_hash=self.task.parameter_hash,
            values=result.values,
            diagnostics=result.diagnostics,
            environment_version=self.task.environment_version,
            runner_image_digest=self.task.runner_image_digest,
            price_basis=training.price_basis,
            frozen_at=frozen_at,
        )
        return self.forecast

    async def claim_simulation(self, _task_id, _test_data, **_kwargs):
        self.calls.append("claim_simulation")

    async def save_success(self, _task_id, _test_data, result, **_kwargs):
        self.calls.append("save_success")
        self.saved_result = result
        return "done"

    async def fail(self, _task_id, code, **_kwargs):
        self.failure_code = code


class ForecastRecorder:
    def __init__(self, order: list[str]) -> None:
        self.requests = []
        self.order = order

    async def forecast(self, request):
        self.order.append("forecast")
        self.requests.append(request)
        return StrategyForecastResult(
            values=TargetValues(
                low_strong="8", low_watch="9", high_watch="11", high_strong="12"
            )
        )


class DataPort:
    def __init__(self, snapshot, label: str, order: list[str]) -> None:
        self.snapshot = snapshot
        self.label = label
        self.order = order

    async def get_training_data(self, **_kwargs):
        self.order.append(self.label)
        return self.snapshot

    async def get_test_data(self, **_kwargs):
        self.order.append(self.label)
        return self.snapshot


def test_holdout_workflow_freezes_forecast_before_loading_test_data() -> None:
    asyncio.run(_run_holdout_workflow())


def test_job_handler_is_a_public_single_backtest_entrypoint() -> None:
    async def scenario() -> None:
        task_id = uuid4()
        execution_id = uuid4()

        class Application:
            called = None

            async def run(self, value, *, execution_token):
                self.called = (value, execution_token)
                return SimpleNamespace(item_status=BacktestItemStatus.SUCCEEDED)

        application = Application()
        handler = build_backtest_job_handler(application)
        result = await handler(
            JobExecutionContext(
                job_id=execution_id,
                fence_token=uuid4(),
                config={"backtest_task_id": str(task_id)},
            )
        )

        assert result.success is True
        assert application.called == (task_id, execution_id)

    asyncio.run(scenario())


def test_terminal_run_short_circuits_without_loading_any_data() -> None:
    async def scenario() -> None:
        task = _task(uuid4())

        class TerminalService:
            async def start(self, _task_id, _execution_token):
                return BacktestExecutionState(
                    task=task,
                    item_id=uuid4(),
                    item_status=BacktestItemStatus.SUCCEEDED,
                    forecast=None,
                )

        application = BacktestApplication(
            FakeDatabase(),
            creation_snapshots=SimpleNamespace(),
            strategy_executions=SimpleNamespace(),
            training_data=SimpleNamespace(),
            test_data=SimpleNamespace(),
            forecasts=SimpleNamespace(),
            adjustments=SimpleNamespace(),
            engine=FixedTargetBacktestEngine(
                BacktestProductionSignalRule(ProductionPriceZoneRule()),
                rule_version=BacktestProductionSignalRule.rule_version,
            ),
            repository_factory=lambda session: session,
            service_factory=lambda _repository, **_kwargs: TerminalService(),
        )

        state = await application.run(task.id, execution_token=uuid4())
        assert state.item_status is BacktestItemStatus.SUCCEEDED

    asyncio.run(scenario())


def test_forecast_race_loser_reuses_the_frozen_forecast() -> None:
    async def scenario() -> None:
        security_id = uuid4()
        task = _task(security_id)
        training = _data(
            security_id,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            marker="train",
        )
        test = _data(
            security_id,
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
            marker="test",
        )
        order = []

        class RacingService(FakeService):
            async def claim_forecast(self, _task_id, value, **_kwargs):
                self.calls.append("claim_forecast")
                self.forecast = _forecast(self.task, self.item_id, value)
                return BacktestExecutionState(
                    task=self.task,
                    item_id=self.item_id,
                    item_status=BacktestItemStatus.FROZEN,
                    forecast=self.forecast,
                )

        service = RacingService(task)
        forecasts = ForecastRecorder(order)
        application = BacktestApplication(
            FakeDatabase(),
            creation_snapshots=SimpleNamespace(),
            strategy_executions=SimpleNamespace(),
            training_data=DataPort(training, "load_training", order),
            test_data=DataPort(test, "load_test", order),
            forecasts=forecasts,
            adjustments=SimpleNamespace(get_adjustment_timeline=_async_value(())),
            engine=FixedTargetBacktestEngine(
                BacktestProductionSignalRule(ProductionPriceZoneRule()),
                rule_version=BacktestProductionSignalRule.rule_version,
            ),
            repository_factory=lambda session: session,
            service_factory=lambda _repository, **_kwargs: service,
        )

        await application.run(task.id, execution_token=uuid4())

        assert forecasts.requests == []
        assert order == ["load_training", "load_test"]

    asyncio.run(scenario())


async def _run_holdout_workflow() -> None:
    security_id = uuid4()
    task = _task(security_id)
    training = _data(
        security_id, start=date(2024, 1, 1), end=date(2024, 12, 31), marker="train"
    )
    test = _data(
        security_id, start=date(2025, 1, 1), end=date(2025, 12, 31), marker="test"
    )
    order: list[str] = []
    service = FakeService(task)
    forecasts = ForecastRecorder(order)
    engine = FixedTargetBacktestEngine(
        BacktestProductionSignalRule(ProductionPriceZoneRule()),
        rule_version=BacktestProductionSignalRule.rule_version,
    )
    application = BacktestApplication(
        FakeDatabase(),
        creation_snapshots=SimpleNamespace(),
        strategy_executions=SimpleNamespace(
            resolve_execution=_async_value(
                SimpleNamespace(strategy_id=uuid4(), source_code="def forecast(): pass")
            )
        ),
        training_data=DataPort(training, "load_training", order),
        test_data=DataPort(test, "load_test", order),
        forecasts=forecasts,
        adjustments=SimpleNamespace(
            get_adjustment_timeline=_async_value(())
        ),
        engine=engine,
        repository_factory=lambda session: session,
        service_factory=lambda _repository, **_kwargs: service,
        clock=lambda: datetime(2026, 7, 21, tzinfo=UTC),
    )

    result = await application.run(task.id, execution_token=uuid4())

    assert result == "done"
    assert order == ["load_training", "forecast", "load_test"]
    assert len(forecasts.requests) == 1
    request = forecasts.requests[0]
    assert request.training_data.rows[0]["marker"] == "train"
    assert "test" not in repr(request)
    assert request.training_data.security_id == security_id
    assert request.training_data.symbol == "600000.SH"
    assert request.security_name == "浦发银行"
    assert service.calls == [
        "start",
        "claim_forecast",
        "freeze_forecast",
        "claim_simulation",
        "save_success",
    ]
    assert service.saved_result.daily_results[0].trade_date == date(2025, 12, 31)


def _task(security_id):
    return BacktestTaskSnapshot(
        id=uuid4(),
        mode=BacktestMode.SINGLE,
        universe_snapshot=(
            BacktestUniverseEntry(
                security_id=security_id, symbol="600000.SH", name="浦发银行"
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
        rule_version=BacktestProductionSignalRule.rule_version,
        hysteresis_ratio=Decimal("0.02"),
        minimum_hysteresis=Decimal("0.02"),
        initial_capital=Decimal("100000"),
        price_basis="QFQ_AS_OF",
        data_source="EASTMONEY",
    )


def _data(security_id, *, start, end, marker):
    return TrainingDataSnapshot(
        security_id=security_id,
        symbol="600000.SH",
        start_date=start,
        end_date=end,
        data_version=1,
        fetched_at=datetime(2026, 7, 21, tzinfo=UTC),
        source="EASTMONEY",
        price_basis="QFQ_AS_OF",
        content_hash=("c" if marker == "train" else "e") * 64,
        rows=(
            {
                "trade_date": end,
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
                "marker": marker,
            },
        ),
    )


def _forecast(task, item_id, training):
    return BacktestForecastSnapshotView(
        item_id=item_id,
        training_start_date=training.start_date,
        training_end_date=training.end_date,
        training_row_count=len(training.rows),
        training_fetched_at=training.fetched_at,
        training_data_hash=training.content_hash,
        source_code_hash=task.source_code_hash,
        parameter_hash=task.parameter_hash,
        values=TargetValues(
            low_strong="8", low_watch="9", high_watch="11", high_strong="12"
        ),
        diagnostics={},
        environment_version=task.environment_version,
        runner_image_digest=task.runner_image_digest,
        price_basis=training.price_basis,
        frozen_at=datetime(2026, 7, 21, tzinfo=UTC),
    )


def _async_value(value):
    async def resolve(*_args, **_kwargs):
        return value

    return resolve
