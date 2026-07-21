from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

from long_invest.modules.backtests.contracts import (
    BacktestErrorCode,
    BacktestForecastSnapshotView,
    BacktestItemStatus,
    BacktestMode,
    BacktestTaskSnapshot,
    BacktestTaskStatus,
    BacktestUniverseEntry,
)
from long_invest.modules.backtests.engine import BacktestEngineResult
from long_invest.modules.backtests.models import (
    BacktestDailyResult,
    BacktestForecastSnapshot,
    BacktestItem,
    BacktestMetric,
    BacktestOrder,
    BacktestTargetAdjustment,
    BacktestTask,
    BacktestTrade,
    BacktestUniverseSnapshot,
)
from long_invest.modules.backtests.repository import forecast_view
from long_invest.modules.strategies.contracts import (
    StrategyForecastResult,
    TrainingDataSnapshot,
)
from long_invest.platform.errors import AppError
from long_invest.platform.json_snapshot import thaw_json_value


@dataclass(frozen=True, slots=True)
class BacktestCommandContext:
    request_id: str
    idempotency_key: str
    actor_user_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class BacktestEvent:
    topic: str
    task_id: UUID
    payload: dict[str, Any]
    dedupe_key: str


class BacktestEventPort(Protocol):
    async def emit(self, event: BacktestEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class BacktestExecutionState:
    task: BacktestTaskSnapshot
    item_id: UUID
    item_status: BacktestItemStatus
    forecast: BacktestForecastSnapshotView | None


class BacktestService:
    def __init__(self, repository, *, events: BacktestEventPort | None = None) -> None:
        self._repository = repository
        self._events = events

    async def create(
        self, snapshot: BacktestTaskSnapshot, context: BacktestCommandContext
    ) -> BacktestExecutionState:
        if snapshot.mode is not BacktestMode.SINGLE:
            raise _error("BACKTEST_MODE_NOT_SUPPORTED", "当前阶段只支持单股回测")
        existing = await self._repository.get_task(snapshot.id, for_update=True)
        if existing is not None:
            state = await self.get_execution(snapshot.id)
            if state.task != snapshot:
                raise _error("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同回测")
            return state
        entry = snapshot.universe_snapshot[0]
        item_id = uuid5(snapshot.id, f"item:{entry.security_id}")
        task = BacktestTask(
            id=snapshot.id,
            mode=snapshot.mode.value,
            status=BacktestTaskStatus.PENDING.value,
            universe_hash=snapshot.universe_hash,
            training_start_date=snapshot.date_range.training_start_date,
            training_end_date=snapshot.date_range.training_end_date,
            test_start_date=snapshot.date_range.test_start_date,
            test_end_date=snapshot.date_range.test_end_date,
            strategy_version_id=snapshot.strategy_version_id,
            draft_id=snapshot.draft_id,
            draft_version=snapshot.draft_version,
            draft_source_code=snapshot.draft_source_code,
            source_code_hash=snapshot.source_code_hash,
            strategy_metadata=thaw_json_value(snapshot.strategy_metadata),
            parameter_schema=thaw_json_value(snapshot.parameter_schema),
            parameter_snapshot=thaw_json_value(snapshot.parameter_snapshot),
            parameter_hash=snapshot.parameter_hash,
            environment_version=snapshot.environment_version,
            runner_image_digest=snapshot.runner_image_digest,
            strategy_api_version=snapshot.strategy_api_version,
            rule_version=snapshot.rule_version,
            hysteresis_ratio=snapshot.hysteresis_ratio,
            minimum_hysteresis=snapshot.minimum_hysteresis,
            initial_capital=snapshot.initial_capital,
            price_basis=snapshot.price_basis,
            data_source=snapshot.data_source,
        )
        universe = BacktestUniverseSnapshot(
            task_id=snapshot.id,
            scope_snapshot=[entry.model_dump(mode="json")],
            content_hash=snapshot.universe_hash,
        )
        item = BacktestItem(
            id=item_id,
            task_id=snapshot.id,
            security_id=entry.security_id,
            status=BacktestItemStatus.PENDING.value,
        )
        await self._repository.add_task(task, universe, item)
        await self._emit(
            "backtest.created",
            snapshot.id,
            {
                "item_id": str(item_id),
                "request_id": context.request_id,
                "actor_user_id": context.actor_user_id,
            },
            f"backtest-created:{snapshot.id}",
        )
        return BacktestExecutionState(
            task=snapshot,
            item_id=item_id,
            item_status=BacktestItemStatus.PENDING,
            forecast=None,
        )

    async def get_execution(self, task_id: UUID) -> BacktestExecutionState:
        task = await self._repository.get_task(task_id)
        item = await self._repository.get_item(task_id)
        universe = await self._repository.get_universe(task_id)
        if task is None or item is None or universe is None:
            raise _not_found()
        forecast = await self._repository.get_forecast(item.id)
        return BacktestExecutionState(
            task=_task_snapshot(task, universe),
            item_id=item.id,
            item_status=BacktestItemStatus(item.status),
            forecast=forecast_view(forecast) if forecast is not None else None,
        )

    async def start(self, task_id: UUID) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id)
        forecast = await self._repository.get_forecast(item.id)
        if item.status == BacktestItemStatus.SUCCEEDED.value:
            return await self.get_execution(task_id)
        if item.status == BacktestItemStatus.FORECASTING.value and forecast is None:
            raise _error(
                BacktestErrorCode.TARGET_REFORECAST_FORBIDDEN.value,
                "上次预测结果未知，禁止再次运行策略",
            )
        if item.status == BacktestItemStatus.FAILED.value:
            raise _error(item.failure_code or "BACKTEST_FAILED", "回测已经失败")
        task.status = BacktestTaskStatus.RUNNING.value
        if forecast is None:
            item.status = BacktestItemStatus.FETCHING_DATA.value
        await self._emit(
            "backtest.started",
            task_id,
            {"item_id": str(item.id)},
            f"backtest-started:{task_id}",
        )
        return BacktestExecutionState(
            task=_task_snapshot(task, universe),
            item_id=item.id,
            item_status=BacktestItemStatus(item.status),
            forecast=forecast_view(forecast) if forecast is not None else None,
        )

    async def claim_forecast(
        self, task_id: UUID, training: TrainingDataSnapshot
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id)
        _validate_data_snapshot(task, item, training, training_period=True)
        existing = await self._repository.get_forecast(item.id)
        if existing is not None:
            return BacktestExecutionState(
                _task_snapshot(task, universe),
                item.id,
                BacktestItemStatus(item.status),
                forecast_view(existing),
            )
        if item.status == BacktestItemStatus.FORECASTING.value:
            raise _error(
                BacktestErrorCode.TARGET_REFORECAST_FORBIDDEN.value,
                "策略预测不能重复运行",
            )
        item.training_data_fetched_at = training.fetched_at
        item.training_data_start_date = training.start_date
        item.training_data_end_date = training.end_date
        item.training_data_row_count = len(training.rows)
        item.training_data_hash = training.content_hash
        item.training_price_basis = training.price_basis
        item.status = BacktestItemStatus.FORECASTING.value
        return BacktestExecutionState(
            _task_snapshot(task, universe),
            item.id,
            BacktestItemStatus.FORECASTING,
            None,
        )

    async def freeze_forecast(
        self,
        task_id: UUID,
        training: TrainingDataSnapshot,
        result: StrategyForecastResult,
        *,
        frozen_at: datetime,
    ) -> BacktestForecastSnapshotView:
        task, item, _ = await self._locked(task_id)
        existing = await self._repository.get_forecast(item.id)
        if existing is not None:
            return forecast_view(existing)
        if item.status != BacktestItemStatus.FORECASTING.value:
            raise _state_conflict()
        _validate_data_snapshot(task, item, training, training_period=True)
        values = result.values
        row = BacktestForecastSnapshot(
            item_id=item.id,
            training_start_date=training.start_date,
            training_end_date=training.end_date,
            training_row_count=len(training.rows),
            training_fetched_at=training.fetched_at,
            training_data_hash=training.content_hash,
            source_code_hash=task.source_code_hash,
            parameter_hash=task.parameter_hash,
            low_strong=values.low_strong,
            low_watch=values.low_watch,
            high_watch=values.high_watch,
            high_strong=values.high_strong,
            diagnostics=thaw_json_value(result.diagnostics),
            environment_version=task.environment_version,
            runner_image_digest=task.runner_image_digest,
            price_basis=training.price_basis,
            frozen_at=frozen_at,
        )
        await self._repository.add_forecast(row)
        item.status = BacktestItemStatus.FROZEN.value
        await self._emit(
            "backtest.forecast_frozen",
            task_id,
            {
                "item_id": str(item.id),
                "forecast_id": str(row.id),
                "training_data_hash": training.content_hash,
            },
            f"backtest-forecast-frozen:{item.id}",
        )
        return forecast_view(row)

    async def claim_simulation(
        self, task_id: UUID, test_data: TrainingDataSnapshot
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id)
        forecast = await self._repository.get_forecast(item.id)
        if forecast is None:
            raise _state_conflict()
        _validate_data_snapshot(task, item, test_data, training_period=False)
        item.test_data_fetched_at = test_data.fetched_at
        item.test_data_start_date = test_data.start_date
        item.test_data_end_date = test_data.end_date
        item.test_data_row_count = len(test_data.rows)
        item.test_data_hash = test_data.content_hash
        item.test_price_basis = test_data.price_basis
        item.status = BacktestItemStatus.SIMULATING.value
        return BacktestExecutionState(
            _task_snapshot(task, universe),
            item.id,
            BacktestItemStatus.SIMULATING,
            forecast_view(forecast),
        )

    async def save_success(
        self,
        task_id: UUID,
        test_data: TrainingDataSnapshot,
        result: BacktestEngineResult,
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id)
        existing_metric = await self._repository.get_metric(item.id)
        if item.status == BacktestItemStatus.SUCCEEDED.value and existing_metric:
            forecast = await self._repository.get_forecast(item.id)
            return BacktestExecutionState(
                _task_snapshot(task, universe),
                item.id,
                BacktestItemStatus.SUCCEEDED,
                forecast_view(forecast),
            )
        if item.status != BacktestItemStatus.SIMULATING.value:
            raise _state_conflict()
        _validate_data_snapshot(task, item, test_data, training_period=False)
        item.status = BacktestItemStatus.SAVING.value
        models = _result_models(result)
        await self._repository.add_results(**models)
        item.status = BacktestItemStatus.SUCCEEDED.value
        item.failure_code = None
        task.status = BacktestTaskStatus.SUCCEEDED.value
        await self._emit_result_events(task, item, result)
        forecast = await self._repository.get_forecast(item.id)
        return BacktestExecutionState(
            _task_snapshot(task, universe),
            item.id,
            BacktestItemStatus.SUCCEEDED,
            forecast_view(forecast),
        )

    async def fail(self, task_id: UUID, code: str) -> None:
        task, item, _ = await self._locked(task_id)
        if item.status == BacktestItemStatus.SUCCEEDED.value:
            return
        item.status = BacktestItemStatus.FAILED.value
        item.failure_code = code
        task.status = BacktestTaskStatus.FAILED.value
        await self._emit(
            "backtest.item_failed",
            task_id,
            {"item_id": str(item.id), "error_code": code},
            f"backtest-item-failed:{item.id}:{code}",
        )
        await self._emit(
            "backtest.completed",
            task_id,
            {"status": BacktestTaskStatus.FAILED.value},
            f"backtest-completed:{task_id}:FAILED",
        )

    async def _locked(self, task_id: UUID):
        task = await self._repository.get_task(task_id, for_update=True)
        item = await self._repository.get_item(task_id, for_update=True)
        universe = await self._repository.get_universe(task_id)
        if task is None or item is None or universe is None:
            raise _not_found()
        return task, item, universe

    async def _emit_result_events(self, task, item, result) -> None:
        for adjustment in result.adjustments:
            await self._emit(
                "backtest.target_adjusted",
                task.id,
                {
                    "item_id": str(item.id),
                    "event_date": adjustment.event_date.isoformat(),
                    "data_hash": adjustment.data_hash,
                },
                f"backtest-target-adjusted:{item.id}:{adjustment.event_date}",
            )
        await self._emit(
            "backtest.item_succeeded",
            task.id,
            {"item_id": str(item.id)},
            f"backtest-item-succeeded:{item.id}",
        )
        await self._emit(
            "backtest.completed",
            task.id,
            {"status": BacktestTaskStatus.SUCCEEDED.value},
            f"backtest-completed:{task.id}:SUCCEEDED",
        )
        await self._emit(
            "strategy.publish_requirement_satisfied",
            task.id,
            {
                "backtest_task_id": str(task.id),
                "backtest_item_id": str(item.id),
                "source_code_hash": task.source_code_hash,
                "parameter_hash": task.parameter_hash,
                "training_data_hash": item.training_data_hash,
                "test_data_hash": item.test_data_hash,
            },
            f"strategy-publish-requirement:{task.id}",
        )

    async def _emit(
        self, topic: str, task_id: UUID, payload: dict[str, Any], dedupe_key: str
    ) -> None:
        if self._events is not None:
            await self._events.emit(BacktestEvent(topic, task_id, payload, dedupe_key))


def _task_snapshot(task, universe) -> BacktestTaskSnapshot:
    from long_invest.modules.backtests.contracts import BacktestDateRange

    return BacktestTaskSnapshot(
        id=task.id,
        mode=task.mode,
        universe_snapshot=tuple(
            BacktestUniverseEntry.model_validate(value)
            for value in universe.scope_snapshot
        ),
        universe_hash=task.universe_hash,
        date_range=BacktestDateRange(
            training_start_date=task.training_start_date,
            training_end_date=task.training_end_date,
            test_start_date=task.test_start_date,
            test_end_date=task.test_end_date,
        ),
        strategy_version_id=task.strategy_version_id,
        draft_id=task.draft_id,
        draft_version=task.draft_version,
        draft_source_code=task.draft_source_code,
        source_code_hash=task.source_code_hash,
        strategy_metadata=task.strategy_metadata,
        parameter_schema=task.parameter_schema,
        parameter_snapshot=task.parameter_snapshot,
        parameter_hash=task.parameter_hash,
        environment_version=task.environment_version,
        runner_image_digest=task.runner_image_digest,
        strategy_api_version=task.strategy_api_version,
        rule_version=task.rule_version,
        hysteresis_ratio=task.hysteresis_ratio,
        minimum_hysteresis=task.minimum_hysteresis,
        initial_capital=task.initial_capital,
        price_basis=task.price_basis,
        data_source=task.data_source,
    )


def _validate_data_snapshot(task, item, data, *, training_period: bool) -> None:
    start = task.training_start_date if training_period else task.test_start_date
    end = task.training_end_date if training_period else task.test_end_date
    if (
        data.security_id != item.security_id
        or data.start_date != start
        or data.end_date != end
    ):
        code = (
            BacktestErrorCode.TRAINING_DATA_INVALID
            if training_period
            else BacktestErrorCode.TEST_DATA_INVALID
        )
        raise _error(code.value, "历史数据与冻结范围不一致")
    if data.price_basis != task.price_basis or data.source != task.data_source:
        raise _error(
            BacktestErrorCode.PRICE_BASIS_MISMATCH.value,
            "训练期和测试期价格口径必须与任务快照一致",
        )


def _result_models(result: BacktestEngineResult) -> dict[str, Any]:
    adjustments = [
        BacktestTargetAdjustment(
            item_id=value.item_id,
            event_date=value.event_date,
            adjustment_factor=value.adjustment_factor,
            **_target_columns(value.before_values, "before_"),
            **_target_columns(value.after_values, "after_"),
            source=value.source,
            data_hash=value.data_hash,
            published_at=value.published_at,
            effective_at=value.effective_at,
        )
        for value in result.adjustments
    ]
    orders = [
        BacktestOrder(
            id=value.id,
            item_id=value.item_id,
            status=value.status.value,
            signal_date=value.signal_date,
            execute_date=value.execute_date,
            direction=value.direction.value,
            execution_price=value.execution_price,
            quantity=value.quantity,
            cash_before=value.cash_before,
            position_before=value.position_before,
            **_target_columns(value.target_values, "target_"),
            target_zone=value.target_zone.value,
        )
        for value in result.orders
    ]
    trades = [
        BacktestTrade(
            id=value.id,
            item_id=value.item_id,
            order_id=value.order_id,
            execute_date=value.execute_date,
            direction=value.direction.value,
            price=value.price,
            quantity=value.quantity,
            cash_after=value.cash_after,
            position_after=value.position_after,
            **_target_columns(value.target_values, "target_"),
            target_zone=value.target_zone.value,
            round_trip_no=value.round_trip_no,
            holding_trade_days=value.holding_trade_days,
            realized_return_amount=value.realized_return_amount,
            realized_return_rate=value.realized_return_rate,
        )
        for value in result.trades
    ]
    daily_results = [
        BacktestDailyResult(
            item_id=value.item_id,
            trade_date=value.trade_date,
            cash=value.cash,
            position_quantity=value.position_quantity,
            close_price=value.close_price,
            position_market_value=value.position_market_value,
            equity=value.equity,
            drawdown=value.drawdown,
            **_target_columns(value.target_values, "target_"),
            zone=value.zone.value,
            position_status=value.position_status.value,
        )
        for value in result.daily_results
    ]
    metric_values = result.metric.model_dump(mode="json")
    content_hash = hashlib.sha256(
        json.dumps(metric_values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    metric = BacktestMetric(
        content_hash=content_hash,
        **result.metric.model_dump(),
    )
    return {
        "adjustments": adjustments,
        "orders": orders,
        "trades": trades,
        "daily_results": daily_results,
        "metric": metric,
    }


def _target_columns(values, prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}low_strong": values.low_strong,
        f"{prefix}low_watch": values.low_watch,
        f"{prefix}high_watch": values.high_watch,
        f"{prefix}high_strong": values.high_strong,
    }


def _error(code: str, message: str) -> AppError:
    return AppError(code=code, message=message, status_code=409)


def _not_found() -> AppError:
    return AppError(code="BACKTEST_NOT_FOUND", message="回测不存在", status_code=404)


def _state_conflict() -> AppError:
    return _error("BACKTEST_STATE_CONFLICT", "回测状态不允许当前操作")
