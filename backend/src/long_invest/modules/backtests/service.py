from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid5

from long_invest.modules.backtests.batch import (
    BacktestBatchItemResult,
    summarize_batch,
)
from long_invest.modules.backtests.contracts import (
    BacktestAction,
    BacktestErrorCode,
    BacktestForecastSnapshotView,
    BacktestItemStatus,
    BacktestItemSummaryView,
    BacktestMode,
    BacktestResultView,
    BacktestSummaryView,
    BacktestTaskListItemView,
    BacktestTaskPage,
    BacktestTaskSnapshot,
    BacktestTaskStatus,
    BacktestTestDataSnapshotView,
    BacktestUniverseEntry,
)
from long_invest.modules.backtests.engine import BacktestEngineResult
from long_invest.modules.backtests.models import (
    BacktestAdjustmentSnapshot,
    BacktestControlCommand,
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
from long_invest.modules.backtests.repository import (
    adjustment_snapshot_view,
    adjustment_view,
    daily_view,
    forecast_view,
    metric_view,
    order_view,
    trade_view,
)
from long_invest.modules.market_data.contracts import AdjustmentTimelineSnapshot
from long_invest.modules.strategies.contracts import (
    StrategyForecastResult,
    TrainingDataSnapshot,
)
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.errors import AppError
from long_invest.platform.json_snapshot import thaw_json_value


@dataclass(frozen=True, slots=True)
class BacktestCommandContext:
    request_id: str
    idempotency_key: str
    actor_user_id: str
    reason: str
    session_id: str | None = None
    trusted_ip: str | None = None


@dataclass(frozen=True, slots=True)
class BacktestEvent:
    topic: str
    task_id: UUID
    payload: dict[str, Any]
    dedupe_key: str


class BacktestEventPort(Protocol):
    async def emit(self, event: BacktestEvent) -> UUID | None: ...


class BacktestAuditPort(Protocol):
    async def append(self, data: AuditWrite) -> Any: ...


@dataclass(frozen=True, slots=True)
class BacktestExecutionState:
    task: BacktestTaskSnapshot
    task_status: BacktestTaskStatus
    execution_generation: int
    item_id: UUID
    item_status: BacktestItemStatus
    forecast: BacktestForecastSnapshotView | None
    adjustment_snapshot: AdjustmentTimelineSnapshot | None = None
    job_id: UUID | None = None


_TERMINAL_ITEM_STATUSES = {
    BacktestItemStatus.SUCCEEDED.value,
    BacktestItemStatus.FAILED.value,
    BacktestItemStatus.SKIPPED.value,
    BacktestItemStatus.CANCELED.value,
}
_TERMINAL_TASK_STATUSES = {
    BacktestTaskStatus.SUCCEEDED.value,
    BacktestTaskStatus.PARTIAL.value,
    BacktestTaskStatus.FAILED.value,
    BacktestTaskStatus.CANCELED.value,
}


class BacktestService:
    def __init__(
        self,
        repository,
        *,
        events: BacktestEventPort | None = None,
        audit: BacktestAuditPort | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._events = events
        self._audit = audit
        self._clock = clock

    async def create(
        self, snapshot: BacktestTaskSnapshot, context: BacktestCommandContext
    ) -> BacktestExecutionState:
        if not context.idempotency_key.strip() or len(context.idempotency_key) > 160:
            raise _error("IDEMPOTENCY_KEY_REQUIRED", "回测需要有效的幂等键")
        request_digest = _request_digest(snapshot)
        replay = await self._repository.get_task_by_idempotency(
            context.idempotency_key, for_update=True
        )
        if replay is not None:
            if replay.request_digest != request_digest:
                raise _error("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同回测")
            state = await self.get_execution(replay.id)
            job_id = await self._emit_created(
                state.task, context, execution_generation=state.execution_generation
            )
            return replace(state, job_id=job_id)
        existing = await self._repository.get_task(snapshot.id, for_update=True)
        if existing is not None:
            raise _error("BACKTEST_TASK_ID_REUSED", "回测编号已被使用")
        if (
            snapshot.mode is BacktestMode.MARKET
            and await self._repository.lock_market_creation()
        ):
            raise _error(
                BacktestErrorCode.BACKTEST_MARKET_ALREADY_RUNNING.value,
                "同一时间只能运行一个全市场回测",
            )
        now = self._clock()
        task = BacktestTask(
            id=snapshot.id,
            mode=snapshot.mode.value,
            status=BacktestTaskStatus.PENDING.value,
            execution_generation=1,
            idempotency_key=context.idempotency_key,
            request_digest=request_digest,
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
            created_at=now,
            updated_at=now,
        )
        universe = BacktestUniverseSnapshot(
            task_id=snapshot.id,
            scope_snapshot=[
                entry.model_dump(mode="json")
                for entry in snapshot.universe_snapshot
            ],
            content_hash=snapshot.universe_hash,
        )
        items = tuple(
            BacktestItem(
                id=uuid5(snapshot.id, f"item:{entry.security_id}"),
                task_id=snapshot.id,
                security_id=entry.security_id,
                status=BacktestItemStatus.PENDING.value,
                attempt_count=0,
            )
            for entry in snapshot.universe_snapshot
        )
        await self._repository.add_task(task, universe, items)
        job_id = await self._emit_created(
            snapshot, context, execution_generation=1
        )
        first = items[0]
        return BacktestExecutionState(
            task=snapshot,
            task_status=BacktestTaskStatus.PENDING,
            execution_generation=1,
            item_id=first.id,
            item_status=BacktestItemStatus.PENDING,
            forecast=None,
            job_id=job_id,
        )

    async def _emit_created(
        self,
        snapshot: BacktestTaskSnapshot,
        context: BacktestCommandContext,
        *,
        execution_generation: int,
    ) -> UUID | None:
        return await self._emit(
            "backtest.created",
            snapshot.id,
            {
                "mode": snapshot.mode.value,
                "item_keys": [entry.symbol for entry in snapshot.universe_snapshot],
                "request_id": context.request_id,
                "actor_user_id": context.actor_user_id,
                "execution_generation": execution_generation,
                "generation": execution_generation,
                "recover": False,
            },
            f"backtest-created:{snapshot.id}",
        )

    async def get_execution(
        self, task_id: UUID, item_id: UUID | None = None
    ) -> BacktestExecutionState:
        task = await self._repository.get_task(task_id)
        item = await self._repository.get_item(task_id, item_id)
        universe = await self._repository.get_universe(task_id)
        if task is None or item is None or universe is None:
            raise _not_found()
        forecast = await self._repository.get_forecast(item.id)
        adjustment_snapshot = await self._repository.get_adjustment_snapshot(item.id)
        return BacktestExecutionState(
            task=_task_snapshot(task, universe),
            task_status=BacktestTaskStatus(task.status),
            execution_generation=task.execution_generation,
            item_id=item.id,
            item_status=BacktestItemStatus(item.status),
            forecast=forecast_view(forecast) if forecast is not None else None,
            adjustment_snapshot=(
                adjustment_snapshot_view(adjustment_snapshot)
                if adjustment_snapshot is not None
                else None
            ),
        )

    async def list_tasks(self, *, page: int, page_size: int) -> BacktestTaskPage:
        rows, total = await self._repository.list_tasks(
            page=page, page_size=page_size
        )
        items = []
        for task, item, universe, _ in rows:
            task_items = await self._repository.list_items(task.id)
            forecast_item_ids = {
                row.id
                for row in task_items
                if await self._repository.get_forecast(row.id) is not None
            }
            items.append(
                _task_list_item(
                    task, item, universe, task_items, forecast_item_ids
                )
            )
        return BacktestTaskPage(
            items=tuple(items),
            page=page,
            page_size=page_size,
            total=total,
        )

    async def list_items(self, task_id: UUID) -> tuple[BacktestItemSummaryView, ...]:
        task = await self._repository.get_task(task_id)
        universe = await self._repository.get_universe(task_id)
        if task is None or universe is None:
            raise _not_found()
        entries = _universe_entries(universe)
        rows = await self._repository.list_items(task_id)
        return tuple(_item_summary(row, entries[row.security_id]) for row in rows)

    async def get_summary(self, task_id: UUID) -> BacktestSummaryView:
        task = await self._repository.get_task(task_id)
        if task is None:
            raise _not_found()
        rows = await self._repository.list_items(task_id)
        if not rows:
            raise _not_found()
        statuses = [BacktestItemStatus(row.status) for row in rows]
        succeeded = statuses.count(BacktestItemStatus.SUCCEEDED)
        failed = statuses.count(BacktestItemStatus.FAILED)
        canceled = statuses.count(BacktestItemStatus.CANCELED)
        completed = succeeded + failed + canceled
        failure_codes: dict[str, int] = {}
        for row in rows:
            if row.failure_code:
                failure_codes[row.failure_code] = (
                    failure_codes.get(row.failure_code, 0) + 1
                )
        metric = await self._repository.get_metric(rows[0].id)
        batch_metric = None
        if task.mode != BacktestMode.SINGLE.value and completed == len(rows):
            universe = await self._repository.get_universe(task_id)
            if universe is None:
                raise _not_found()
            entries = _universe_entries(universe)
            batch_results = []
            for row in rows:
                row_metric = await self._repository.get_metric(row.id)
                batch_results.append(
                    BacktestBatchItemResult(
                        entry=entries[row.security_id],
                        metric=(
                            metric_view(row_metric) if row_metric is not None else None
                        ),
                        failure_code=(
                            None
                            if row_metric is not None
                            else row.failure_code or "BACKTEST_ITEM_CANCELED"
                        ),
                    )
                )
            batch_metric = summarize_batch(tuple(batch_results))
        forecast_item_ids = {
            row.id
            for row in rows
            if await self._repository.get_forecast(row.id) is not None
        }
        return BacktestSummaryView(
            task_id=task.id,
            status=task.status,
            survivor_bias_disclosed=(task.mode == BacktestMode.MARKET.value),
            total_items=len(rows),
            completed_items=completed,
            succeeded_items=succeeded,
            failed_items=failed,
            canceled_items=canceled,
            pending_items=len(rows) - completed,
            failure_codes=failure_codes,
            allowed_actions=_allowed_actions(task, rows, forecast_item_ids),
            metric=(
                metric_view(metric)
                if metric is not None and task.mode == BacktestMode.SINGLE.value
                else None
            ),
            batch_metric=batch_metric,
        )

    async def pause(
        self, task_id: UUID, context: BacktestCommandContext
    ) -> BacktestExecutionState:
        replay = await self._control_replay(task_id, BacktestAction.PAUSE, context)
        if replay is not None:
            return replay
        task, items, universe = await self._locked_items(task_id)
        item = items[0]
        completed_immediately = False
        has_active_items = any(row.execution_token is not None for row in items)
        if task.status == BacktestTaskStatus.PENDING.value or not has_active_items:
            task.status = BacktestTaskStatus.PAUSED.value
            completed_immediately = True
        elif task.status == BacktestTaskStatus.RUNNING.value:
            task.status = BacktestTaskStatus.PAUSING.value
        elif task.status not in {
            BacktestTaskStatus.PAUSING.value,
            BacktestTaskStatus.PAUSED.value,
        }:
            raise _state_conflict()
        task.updated_at = self._clock()
        await self._record_control(task_id, BacktestAction.PAUSE, context, task_id)
        if completed_immediately:
            await self._emit_control("backtest.paused", task, item, context)
        return await self._state(task, item, universe)

    async def resume(
        self, task_id: UUID, context: BacktestCommandContext
    ) -> BacktestExecutionState:
        replay = await self._control_replay(task_id, BacktestAction.RESUME, context)
        if replay is not None:
            return replay
        task, items, universe = await self._locked_items(task_id)
        item = items[0]
        if task.status != BacktestTaskStatus.PAUSED.value:
            raise _state_conflict()
        task.status = BacktestTaskStatus.PENDING.value
        task.execution_generation += 1
        task.updated_at = self._clock()
        unfinished = [
            row for row in items if row.status not in _TERMINAL_ITEM_STATUSES
        ]
        for row in unfinished:
            row.execution_token = None
            row.ended_at = None
        await self._record_control(task_id, BacktestAction.RESUME, context, task_id)
        await self._emit_control(
            "backtest.resumed",
            task,
            item,
            context,
            items=unfinished,
            universe=universe,
        )
        return await self._state(task, item, universe)

    async def cancel(
        self, task_id: UUID, context: BacktestCommandContext
    ) -> BacktestExecutionState:
        replay = await self._control_replay(task_id, BacktestAction.CANCEL, context)
        if replay is not None:
            return replay
        task, items, universe = await self._locked_items(task_id)
        item = items[0]
        now = self._clock()
        completed_immediately = False
        if task.status in {
            BacktestTaskStatus.PENDING.value,
            BacktestTaskStatus.PAUSED.value,
        }:
            for row in items:
                if row.status not in _TERMINAL_ITEM_STATUSES:
                    _cancel_item(row, now)
            _finish_canceled_task(task, now)
            completed_immediately = True
        elif task.status in {
            BacktestTaskStatus.RUNNING.value,
            BacktestTaskStatus.PAUSING.value,
        }:
            task.status = BacktestTaskStatus.CANCELING.value
            task.updated_at = now
            for row in items:
                if (
                    row.status not in _TERMINAL_ITEM_STATUSES
                    and row.execution_token is None
                ):
                    _cancel_item(row, now)
            if all(row.status in _TERMINAL_ITEM_STATUSES for row in items):
                _finish_canceled_task(task, now)
                completed_immediately = True
        elif task.status != BacktestTaskStatus.CANCELED.value:
            raise _state_conflict()
        await self._record_control(task_id, BacktestAction.CANCEL, context, task_id)
        if completed_immediately:
            await self._emit_control("backtest.canceled", task, item, context)
        return await self._state(task, item, universe)

    async def retry_failed(
        self, task_id: UUID, context: BacktestCommandContext
    ) -> BacktestExecutionState:
        replay = await self._control_replay(
            task_id, BacktestAction.RETRY_FAILED, context
        )
        if replay is not None:
            return replay
        task, items, universe = await self._locked_items(task_id)
        if task.status not in {
            BacktestTaskStatus.FAILED.value,
            BacktestTaskStatus.PARTIAL.value,
        }:
            raise _state_conflict()
        failed_items = [
            row for row in items if row.status == BacktestItemStatus.FAILED.value
        ]
        if not failed_items:
            raise _state_conflict()
        forecasts = {
            row.id: await self._repository.get_forecast(row.id)
            for row in failed_items
        }
        if any(
            forecasts[row.id] is None and row.training_data_hash is not None
            for row in failed_items
        ):
            raise _error(
                "BACKTEST_NOT_RECOVERABLE", "策略执行结果未知，当前回测不能重试"
            )
        now = self._clock()
        task.status = BacktestTaskStatus.PENDING.value
        task.execution_generation += 1
        task.updated_at = now
        task.terminal_at = None
        for row in failed_items:
            row.status = (
                BacktestItemStatus.FROZEN.value
                if forecasts[row.id] is not None
                else BacktestItemStatus.PENDING.value
            )
            row.failure_code = None
            row.execution_token = None
            row.ended_at = None
        await self._record_control(
            task_id, BacktestAction.RETRY_FAILED, context, task_id
        )
        await self._emit_control(
            "backtest.resumed",
            task,
            failed_items[0],
            context,
            items=failed_items,
            universe=universe,
        )
        return await self._state(task, failed_items[0], universe)

    async def rerun(
        self,
        task_id: UUID,
        new_task_id: UUID,
        context: BacktestCommandContext,
    ) -> BacktestExecutionState:
        replay = await self._control_replay(task_id, BacktestAction.RERUN, context)
        if replay is not None:
            return replay
        source, _, source_universe = await self._locked(task_id)
        if source.status not in _TERMINAL_TASK_STATUSES:
            raise _state_conflict()
        if await self._repository.get_task(new_task_id, for_update=True) is not None:
            raise _error("BACKTEST_TASK_ID_REUSED", "回测编号已被使用")
        snapshot = _task_snapshot(source, source_universe).model_copy(
            update={"id": new_task_id}
        )
        now = self._clock()
        task = _rerun_task(source, new_task_id, context, now)
        items = tuple(
            BacktestItem(
                id=uuid5(new_task_id, f"item:{entry.security_id}"),
                task_id=new_task_id,
                security_id=entry.security_id,
                status=BacktestItemStatus.PENDING.value,
                attempt_count=0,
            )
            for entry in snapshot.universe_snapshot
        )
        universe = BacktestUniverseSnapshot(
            task_id=new_task_id,
            scope_snapshot=list(source_universe.scope_snapshot),
            content_hash=source_universe.content_hash,
        )
        await self._repository.add_task(task, universe, items)
        await self._record_control(task_id, BacktestAction.RERUN, context, new_task_id)
        await self._emit(
            "backtest.created",
            new_task_id,
            {
                "mode": snapshot.mode.value,
                "item_keys": [entry.symbol for entry in snapshot.universe_snapshot],
                "request_id": context.request_id,
                "actor_user_id": context.actor_user_id,
                "execution_generation": 1,
                "generation": 1,
                "recover": False,
                "rerun_from_task_id": str(task_id),
            },
            f"backtest-created:{new_task_id}",
        )
        return BacktestExecutionState(
            task=snapshot,
            task_status=BacktestTaskStatus.PENDING,
            execution_generation=1,
            item_id=items[0].id,
            item_status=BacktestItemStatus.PENDING,
            forecast=None,
        )

    async def get_result(self, task_id: UUID, item_id: UUID) -> BacktestResultView:
        item = await self._repository.get_item_by_id(task_id, item_id)
        if item is None:
            raise _not_found()
        forecast = await self._repository.get_forecast(item.id)
        adjustment_snapshot = await self._repository.get_adjustment_snapshot(item.id)
        metric = await self._repository.get_metric(item.id)
        adjustments = await self._repository.list_adjustments(item.id)
        orders = await self._repository.list_orders(item.id)
        trades = await self._repository.list_trades(item.id)
        daily = await self._repository.list_daily_results(item.id)
        return BacktestResultView(
            task_id=task_id,
            item_id=item.id,
            item_status=BacktestItemStatus(item.status),
            forecast=forecast_view(forecast) if forecast is not None else None,
            test_data_snapshot=_test_data_snapshot(item),
            adjustment_snapshot=(
                adjustment_snapshot_view(adjustment_snapshot)
                if adjustment_snapshot is not None
                else None
            ),
            adjustments=tuple(adjustment_view(row) for row in adjustments),
            orders=tuple(order_view(row) for row in orders),
            trades=tuple(trade_view(row) for row in trades),
            daily_results=tuple(daily_view(row) for row in daily),
            metric=metric_view(metric) if metric is not None else None,
        )

    async def start(
        self,
        task_id: UUID,
        execution_token: UUID,
        *,
        expected_generation: int,
        item_id: UUID | None = None,
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id, item_id)
        _require_generation(task, expected_generation)
        forecast = await self._repository.get_forecast(item.id)
        adjustment_snapshot = await self._repository.get_adjustment_snapshot(item.id)
        if task.status in {
            BacktestTaskStatus.PAUSED.value,
            BacktestTaskStatus.CANCELED.value,
            BacktestTaskStatus.SUCCEEDED.value,
            BacktestTaskStatus.FAILED.value,
        }:
            return await self._state(task, item, universe)
        if task.status not in {
            BacktestTaskStatus.PENDING.value,
            BacktestTaskStatus.RUNNING.value,
        }:
            raise _state_conflict()
        if item.status in _TERMINAL_ITEM_STATUSES:
            return _execution_state(task, item, universe, forecast)
        if item.execution_token is not None and item.execution_token != execution_token:
            raise _error("BACKTEST_ALREADY_RUNNING", "回测已由另一个执行器处理")
        if item.status == BacktestItemStatus.FORECASTING.value and forecast is None:
            raise _error(
                BacktestErrorCode.TARGET_REFORECAST_FORBIDDEN.value,
                "上次预测结果未知，禁止再次运行策略",
            )
        if item.execution_token is None:
            item.attempt_count += 1
            item.started_at = item.started_at or self._clock()
        item.execution_token = execution_token
        task.status = BacktestTaskStatus.RUNNING.value
        task.updated_at = self._clock()
        if forecast is None:
            item.status = BacktestItemStatus.FETCHING_DATA.value
        elif item.status in {
            BacktestItemStatus.SIMULATING.value,
            BacktestItemStatus.SAVING.value,
        }:
            item.status = BacktestItemStatus.FROZEN.value
        await self._emit(
            "backtest.started",
            task_id,
            {"item_id": str(item.id)},
            f"backtest-started:{task_id}",
        )
        return BacktestExecutionState(
            task=_task_snapshot(task, universe),
            task_status=BacktestTaskStatus(task.status),
            execution_generation=task.execution_generation,
            item_id=item.id,
            item_status=BacktestItemStatus(item.status),
            forecast=forecast_view(forecast) if forecast is not None else None,
            adjustment_snapshot=(
                adjustment_snapshot_view(adjustment_snapshot)
                if adjustment_snapshot is not None
                else None
            ),
        )

    async def claim_forecast(
        self,
        task_id: UUID,
        training: TrainingDataSnapshot,
        *,
        execution_token: UUID,
        item_id: UUID | None = None,
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id, item_id)
        _require_execution_owner(item, execution_token)
        _validate_data_snapshot(task, item, training, training_period=True)
        existing = await self._repository.get_forecast(item.id)
        if existing is not None:
            return BacktestExecutionState(
                task=_task_snapshot(task, universe),
                task_status=BacktestTaskStatus(task.status),
                execution_generation=task.execution_generation,
                item_id=item.id,
                item_status=BacktestItemStatus(item.status),
                forecast=forecast_view(existing),
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
            task=_task_snapshot(task, universe),
            task_status=BacktestTaskStatus(task.status),
            execution_generation=task.execution_generation,
            item_id=item.id,
            item_status=BacktestItemStatus.FORECASTING,
            forecast=None,
        )

    async def freeze_forecast(
        self,
        task_id: UUID,
        training: TrainingDataSnapshot,
        result: StrategyForecastResult,
        *,
        execution_token: UUID,
        frozen_at: datetime,
        item_id: UUID | None = None,
    ) -> BacktestForecastSnapshotView:
        task, item, _ = await self._locked(task_id, item_id)
        _require_execution_owner(item, execution_token)
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
        self,
        task_id: UUID,
        test_data: TrainingDataSnapshot,
        *,
        execution_token: UUID,
        item_id: UUID | None = None,
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id, item_id)
        _require_execution_owner(item, execution_token)
        forecast = await self._repository.get_forecast(item.id)
        if forecast is None:
            raise _state_conflict()
        _validate_data_snapshot(task, item, test_data, training_period=False)
        if item.test_data_hash is not None:
            _validate_frozen_test_snapshot(item, test_data)
        else:
            item.test_data_fetched_at = test_data.fetched_at
            item.test_data_start_date = test_data.start_date
            item.test_data_end_date = test_data.end_date
            item.test_data_row_count = len(test_data.rows)
            item.test_data_hash = test_data.content_hash
            item.test_price_basis = test_data.price_basis
        item.status = BacktestItemStatus.SIMULATING.value
        return BacktestExecutionState(
            task=_task_snapshot(task, universe),
            task_status=BacktestTaskStatus(task.status),
            execution_generation=task.execution_generation,
            item_id=item.id,
            item_status=BacktestItemStatus.SIMULATING,
            forecast=forecast_view(forecast),
        )

    async def freeze_adjustment_snapshot(
        self,
        task_id: UUID,
        timeline: AdjustmentTimelineSnapshot,
        *,
        execution_token: UUID,
        item_id: UUID | None = None,
    ) -> AdjustmentTimelineSnapshot:
        task, item, _ = await self._locked(task_id, item_id)
        _require_execution_owner(item, execution_token)
        existing = await self._repository.get_adjustment_snapshot(item.id)
        if existing is not None:
            frozen = adjustment_snapshot_view(existing)
            if frozen != timeline:
                raise _error(
                    "BACKTEST_ADJUSTMENT_SNAPSHOT_CONFLICT",
                    "公司行动输入已经冻结，不能替换",
                )
            return frozen
        if item.status != BacktestItemStatus.SIMULATING.value:
            raise _state_conflict()
        if (
            timeline.security_id != item.security_id
            or timeline.start_date != task.training_end_date + timedelta(days=1)
            or timeline.end_date != task.test_end_date
            or timeline.as_of < item.test_data_fetched_at
        ):
            raise _error(
                BacktestErrorCode.TEST_DATA_INVALID.value,
                "公司行动快照与回测范围不一致",
            )
        row = BacktestAdjustmentSnapshot(
            item_id=item.id,
            source_snapshot_id=timeline.snapshot_id,
            security_id=timeline.security_id,
            start_date=timeline.start_date,
            end_date=timeline.end_date,
            as_of=timeline.as_of,
            source=timeline.source,
            provider_contract_version=timeline.provider_contract_version,
            fetched_at=timeline.fetched_at,
            row_count=timeline.row_count,
            content_hash=timeline.content_hash,
            entries=[
                {
                    "event_date": entry.event_date.isoformat(),
                    "effective_date": entry.effective_date.isoformat(),
                    "published_at": entry.published_at.isoformat(),
                    "source": entry.source,
                    "adjustment_factor": str(entry.adjustment_factor),
                    "data_hash": entry.data_hash,
                }
                for entry in timeline.entries
            ],
        )
        await self._repository.add_adjustment_snapshot(row)
        return adjustment_snapshot_view(row)

    async def save_success(
        self,
        task_id: UUID,
        test_data: TrainingDataSnapshot,
        result: BacktestEngineResult,
        *,
        execution_token: UUID,
        item_id: UUID | None = None,
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id, item_id)
        _require_execution_owner(item, execution_token)
        if task.status == BacktestTaskStatus.PAUSING.value:
            completed = await self._pause_item(task, item, self._clock())
            if completed:
                await self._emit(
                    "backtest.paused",
                    task_id,
                    {"execution_generation": task.execution_generation},
                    f"backtest-paused:{task_id}:{task.execution_generation}",
                )
            return await self._state(task, item, universe)
        if task.status == BacktestTaskStatus.CANCELING.value:
            completed = await self._cancel_item(task, item, self._clock())
            if completed:
                await self._emit(
                    "backtest.canceled",
                    task_id,
                    {"execution_generation": task.execution_generation},
                    f"backtest-canceled:{task_id}:{task.execution_generation}",
                )
            return await self._state(task, item, universe)
        existing_metric = await self._repository.get_metric(item.id)
        if item.status == BacktestItemStatus.SUCCEEDED.value and existing_metric:
            forecast = await self._repository.get_forecast(item.id)
            return BacktestExecutionState(
                task=_task_snapshot(task, universe),
                task_status=BacktestTaskStatus(task.status),
                execution_generation=task.execution_generation,
                item_id=item.id,
                item_status=BacktestItemStatus.SUCCEEDED,
                forecast=forecast_view(forecast),
            )
        if item.status != BacktestItemStatus.SIMULATING.value:
            raise _state_conflict()
        _validate_data_snapshot(task, item, test_data, training_period=False)
        item.status = BacktestItemStatus.SAVING.value
        models = _result_models(result)
        await self._repository.add_results(**models)
        item.status = BacktestItemStatus.SUCCEEDED.value
        item.failure_code = None
        item.execution_token = None
        item.ended_at = self._clock()
        await self._refresh_parent_status(task)
        await self._emit_result_events(task, item, result)
        forecast = await self._repository.get_forecast(item.id)
        return BacktestExecutionState(
            task=_task_snapshot(task, universe),
            task_status=BacktestTaskStatus(task.status),
            execution_generation=task.execution_generation,
            item_id=item.id,
            item_status=BacktestItemStatus.SUCCEEDED,
            forecast=forecast_view(forecast),
        )

    async def fail(
        self,
        task_id: UUID,
        code: str,
        *,
        execution_token: UUID,
        item_id: UUID | None = None,
    ) -> None:
        task, item, _ = await self._locked(task_id, item_id)
        if item.status in _TERMINAL_ITEM_STATUSES:
            return
        if item.execution_token != execution_token:
            return
        item.status = BacktestItemStatus.FAILED.value
        item.failure_code = code
        item.execution_token = None
        item.ended_at = self._clock()
        await self._refresh_parent_status(task)
        await self._emit(
            "backtest.item_failed",
            task_id,
            {"item_id": str(item.id), "error_code": code},
            f"backtest-item-failed:{item.id}:{code}",
        )
        if task.status in _TERMINAL_TASK_STATUSES:
            await self._emit(
                "backtest.completed",
                task_id,
                {"status": task.status},
                f"backtest-completed:{task_id}:{task.status}",
            )

    async def recover(
        self,
        task_id: UUID,
        *,
        execution_token: UUID,
        expected_generation: int,
        item_id: UUID | None = None,
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id, item_id)
        _require_generation(task, expected_generation)
        if task.status != BacktestTaskStatus.PENDING.value:
            if item.execution_token == execution_token:
                forecast = await self._repository.get_forecast(item.id)
                return _execution_state(task, item, universe, forecast)
            raise _error("BACKTEST_ALREADY_RUNNING", "回测已由另一个执行器处理")
        if item.execution_token is not None and item.execution_token != execution_token:
            raise _error("BACKTEST_ALREADY_RUNNING", "回测已由另一个执行器处理")
        forecast = await self._repository.get_forecast(item.id)
        recoverable_without_forecast = item.status in {
            BacktestItemStatus.PENDING.value,
            BacktestItemStatus.FETCHING_DATA.value,
            BacktestItemStatus.VALIDATING_DATA.value,
        }
        recoverable_with_forecast = forecast is not None and item.status in {
            BacktestItemStatus.FROZEN.value,
            BacktestItemStatus.SIMULATING.value,
            BacktestItemStatus.SAVING.value,
            BacktestItemStatus.FAILED.value,
        }
        if not (recoverable_without_forecast or recoverable_with_forecast):
            raise _error("BACKTEST_NOT_RECOVERABLE", "当前回测不能恢复")
        item.execution_token = execution_token
        item.attempt_count += 1
        item.started_at = item.started_at or self._clock()
        item.status = (
            BacktestItemStatus.FROZEN.value
            if forecast is not None
            else BacktestItemStatus.FETCHING_DATA.value
        )
        item.failure_code = None
        task.status = BacktestTaskStatus.RUNNING.value
        task.updated_at = self._clock()
        await self._emit(
            "backtest.resumed",
            task_id,
            {"item_id": str(item.id)},
            f"backtest-resumed:{task_id}:{execution_token}",
        )
        return _execution_state(task, item, universe, forecast)

    async def checkpoint(
        self,
        task_id: UUID,
        execution_token: UUID,
        *,
        expected_generation: int,
        item_id: UUID | None = None,
    ) -> BacktestExecutionState:
        task, item, universe = await self._locked(task_id, item_id)
        _require_generation(task, expected_generation)
        _require_execution_owner(item, execution_token)
        now = self._clock()
        if task.status == BacktestTaskStatus.PAUSING.value:
            completed = await self._pause_item(task, item, now)
            if completed:
                await self._emit(
                    "backtest.paused",
                    task_id,
                    {"execution_generation": expected_generation},
                    f"backtest-paused:{task_id}:{expected_generation}",
                )
        elif task.status == BacktestTaskStatus.CANCELING.value:
            completed = await self._cancel_item(task, item, now)
            if completed:
                await self._emit(
                    "backtest.canceled",
                    task_id,
                    {"execution_generation": expected_generation},
                    f"backtest-canceled:{task_id}:{expected_generation}",
                )
        return await self._state(task, item, universe)

    async def _control_replay(
        self,
        task_id: UUID,
        action: BacktestAction,
        context: BacktestCommandContext,
    ) -> BacktestExecutionState | None:
        _require_context(context)
        command = await self._repository.get_control_by_idempotency(
            context.idempotency_key, for_update=True
        )
        if command is None:
            return None
        digest = _control_digest(task_id, action, context)
        if command.request_digest != digest:
            raise _error("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同回测操作")
        return await self.get_execution(command.result_task_id or command.task_id)

    async def _record_control(
        self,
        task_id: UUID,
        action: BacktestAction,
        context: BacktestCommandContext,
        result_task_id: UUID,
    ) -> None:
        await self._repository.add_control(
            BacktestControlCommand(
                task_id=task_id,
                action=action.value,
                idempotency_key=context.idempotency_key,
                request_digest=_control_digest(task_id, action, context),
                result_task_id=result_task_id,
                created_at=self._clock(),
            )
        )
        if self._audit is not None:
            result = await self.get_execution(result_task_id)
            await self._audit.append(
                AuditWrite(
                    action_code=f"BACKTEST_{action.value}",
                    object_type="backtest_task",
                    object_id=str(task_id),
                    result="SUCCESS",
                    request_id=context.request_id,
                    idempotency_key=(
                        "backtest-control:"
                        + hashlib.sha256(context.idempotency_key.encode()).hexdigest()
                    ),
                    risk_level="HIGH",
                    reason=context.reason.strip(),
                    before_summary=None,
                    after_summary={
                        "result_task_id": str(result_task_id),
                        "status": result.task_status.value,
                        "execution_generation": result.execution_generation,
                    },
                    actor_user_id=context.actor_user_id,
                    session_id=context.session_id,
                    trusted_ip=context.trusted_ip,
                )
            )

    async def _emit_control(
        self,
        topic: str,
        task,
        item,
        context: BacktestCommandContext,
        *,
        items=None,
        universe=None,
    ) -> None:
        payload = {
            "item_id": str(item.id),
            "status": task.status,
            "execution_generation": task.execution_generation,
            "generation": task.execution_generation,
            "recover": item.status != BacktestItemStatus.PENDING.value,
            "request_id": context.request_id,
            "actor_user_id": context.actor_user_id,
            "reason": context.reason,
        }
        if task.mode != BacktestMode.SINGLE.value and items is not None:
            entries = _universe_entries(universe)
            payload.update(
                {
                    "mode": task.mode,
                    "item_keys": [entries[row.security_id].symbol for row in items],
                    "recover": True,
                }
            )
        await self._emit(
            topic,
            task.id,
            payload,
            f"{topic}:{task.id}:{context.idempotency_key}",
        )

    async def _state(self, task, item, universe) -> BacktestExecutionState:
        forecast = await self._repository.get_forecast(item.id)
        adjustment = await self._repository.get_adjustment_snapshot(item.id)
        return BacktestExecutionState(
            task=_task_snapshot(task, universe),
            task_status=BacktestTaskStatus(task.status),
            execution_generation=task.execution_generation,
            item_id=item.id,
            item_status=BacktestItemStatus(item.status),
            forecast=forecast_view(forecast) if forecast is not None else None,
            adjustment_snapshot=(
                adjustment_snapshot_view(adjustment)
                if adjustment is not None
                else None
            ),
        )

    async def _locked(self, task_id: UUID, item_id: UUID | None = None):
        task = await self._repository.get_task(task_id, for_update=True)
        item = await self._repository.get_item(
            task_id, item_id, for_update=True
        )
        universe = await self._repository.get_universe(task_id)
        if task is None or item is None or universe is None:
            raise _not_found()
        return task, item, universe

    async def _locked_items(self, task_id: UUID):
        task = await self._repository.get_task(task_id, for_update=True)
        items = await self._repository.list_items(task_id, for_update=True)
        universe = await self._repository.get_universe(task_id)
        if task is None or not items or universe is None:
            raise _not_found()
        return task, items, universe

    async def _pause_item(self, task, item, now: datetime) -> bool:
        item.execution_token = None
        items = await self._repository.list_items(task.id, for_update=True)
        if any(row.execution_token is not None for row in items):
            task.updated_at = now
            return False
        task.status = BacktestTaskStatus.PAUSED.value
        task.updated_at = now
        return True

    async def _cancel_item(self, task, item, now: datetime) -> bool:
        _cancel_item(item, now)
        items = await self._repository.list_items(task.id, for_update=True)
        if any(row.status not in _TERMINAL_ITEM_STATUSES for row in items):
            task.updated_at = now
            return False
        _finish_canceled_task(task, now)
        return True

    async def _refresh_parent_status(self, task) -> None:
        items = await self._repository.list_items(task.id)
        statuses = {BacktestItemStatus(item.status) for item in items}
        terminal = statuses <= _TERMINAL_ITEM_STATUSES
        now = self._clock()
        task.updated_at = now
        if not terminal:
            task.status = BacktestTaskStatus.RUNNING.value
            task.terminal_at = None
            return
        succeeded = BacktestItemStatus.SUCCEEDED in statuses
        failed = bool(
            statuses
            & {
                BacktestItemStatus.FAILED,
                BacktestItemStatus.SKIPPED,
                BacktestItemStatus.CANCELED,
            }
        )
        if succeeded and failed:
            task.status = BacktestTaskStatus.PARTIAL.value
        elif succeeded:
            task.status = BacktestTaskStatus.SUCCEEDED.value
        else:
            task.status = BacktestTaskStatus.FAILED.value
        task.terminal_at = now

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
        if task.status in _TERMINAL_TASK_STATUSES:
            await self._emit(
                "backtest.completed",
                task.id,
                {"status": task.status},
                f"backtest-completed:{task.id}:{task.status}",
            )
        if task.mode == BacktestMode.SINGLE.value:
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
    ) -> UUID | None:
        if self._events is not None:
            return await self._events.emit(
                BacktestEvent(topic, task_id, payload, dedupe_key)
            )
        return None


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
        survivor_bias_disclosed=(task.mode == BacktestMode.MARKET.value),
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


def _execution_state(task, item, universe, forecast) -> BacktestExecutionState:
    return BacktestExecutionState(
        task=_task_snapshot(task, universe),
        task_status=BacktestTaskStatus(task.status),
        execution_generation=task.execution_generation,
        item_id=item.id,
        item_status=BacktestItemStatus(item.status),
        forecast=forecast_view(forecast) if forecast is not None else None,
    )


def _task_list_item(
    task, item, universe, items, forecast_item_ids: set[UUID]
) -> BacktestTaskListItemView:
    entries = _universe_entries(universe)
    return BacktestTaskListItemView(
        task_id=task.id,
        rerun_from_task_id=task.rerun_from_task_id,
        mode=task.mode,
        status=task.status,
        strategy_version_id=task.strategy_version_id,
        draft_id=task.draft_id,
        draft_version=task.draft_version,
        date_range={
            "training_start_date": task.training_start_date,
            "training_end_date": task.training_end_date,
            "test_start_date": task.test_start_date,
            "test_end_date": task.test_end_date,
        },
        item=_item_summary(item, entries[item.security_id]),
        allowed_actions=_allowed_actions(task, items, forecast_item_ids),
        created_at=task.created_at,
        updated_at=task.updated_at,
        terminal_at=task.terminal_at,
    )


def _item_summary(item, entry: BacktestUniverseEntry) -> BacktestItemSummaryView:
    return BacktestItemSummaryView(
        item_id=item.id,
        security_id=item.security_id,
        symbol=entry.symbol,
        name=entry.name,
        status=item.status,
        failure_code=item.failure_code,
        attempt_count=item.attempt_count,
        started_at=item.started_at,
        ended_at=item.ended_at,
    )


def _universe_entries(universe) -> dict[UUID, BacktestUniverseEntry]:
    entries = tuple(
        BacktestUniverseEntry.model_validate(value) for value in universe.scope_snapshot
    )
    return {entry.security_id: entry for entry in entries}


def _allowed_actions(
    task, items, forecast_item_ids: set[UUID]
) -> tuple[BacktestAction, ...]:
    status = BacktestTaskStatus(task.status)
    if status is BacktestTaskStatus.PENDING:
        return (BacktestAction.PAUSE, BacktestAction.CANCEL)
    if status is BacktestTaskStatus.RUNNING:
        return (BacktestAction.PAUSE, BacktestAction.CANCEL)
    if status is BacktestTaskStatus.PAUSING:
        return (BacktestAction.CANCEL,)
    if status is BacktestTaskStatus.PAUSED:
        return (BacktestAction.RESUME, BacktestAction.CANCEL)
    if status in {BacktestTaskStatus.FAILED, BacktestTaskStatus.PARTIAL}:
        failed = [
            item for item in items if item.status == BacktestItemStatus.FAILED.value
        ]
        retryable = bool(failed) and all(
            item.id in forecast_item_ids or item.training_data_hash is None
            for item in failed
        )
        return (
            (BacktestAction.RETRY_FAILED, BacktestAction.RERUN)
            if retryable
            else (BacktestAction.RERUN,)
        )
    if status in {
        BacktestTaskStatus.SUCCEEDED,
        BacktestTaskStatus.CANCELED,
    }:
        return (BacktestAction.RERUN,)
    return ()


def _require_generation(task, expected_generation: int) -> None:
    if task.execution_generation != expected_generation:
        raise _error("BACKTEST_EXECUTION_SUPERSEDED", "回测执行代数已经失效")


def _finish_canceled_task(task, now: datetime) -> None:
    task.status = BacktestTaskStatus.CANCELED.value
    task.updated_at = now
    task.terminal_at = now


def _cancel_item(item, now: datetime) -> None:
    item.status = BacktestItemStatus.CANCELED.value
    item.failure_code = None
    item.execution_token = None
    item.ended_at = now


def _require_context(context: BacktestCommandContext) -> None:
    if not context.idempotency_key.strip() or len(context.idempotency_key) > 160:
        raise _error("IDEMPOTENCY_KEY_REQUIRED", "回测操作需要有效的幂等键")
    if not context.reason.strip():
        raise _error("BACKTEST_INPUT_INVALID", "操作原因不能为空")


def _control_digest(
    task_id: UUID, action: BacktestAction, context: BacktestCommandContext
) -> str:
    payload = {
        "task_id": str(task_id),
        "action": action.value,
        "reason": context.reason.strip(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _rerun_task(
    source,
    new_task_id: UUID,
    context: BacktestCommandContext,
    now: datetime,
) -> BacktestTask:
    values = {
        column: getattr(source, column)
        for column in (
            "mode",
            "universe_hash",
            "training_start_date",
            "training_end_date",
            "test_start_date",
            "test_end_date",
            "strategy_version_id",
            "draft_id",
            "draft_version",
            "draft_source_code",
            "source_code_hash",
            "strategy_metadata",
            "parameter_schema",
            "parameter_snapshot",
            "parameter_hash",
            "environment_version",
            "runner_image_digest",
            "strategy_api_version",
            "rule_version",
            "hysteresis_ratio",
            "minimum_hysteresis",
            "price_basis",
            "data_source",
            "initial_capital",
        )
    }
    return BacktestTask(
        id=new_task_id,
        status=BacktestTaskStatus.PENDING.value,
        execution_generation=1,
        rerun_from_task_id=source.id,
        idempotency_key=(
            "rerun:" + hashlib.sha256(context.idempotency_key.encode()).hexdigest()
        ),
        request_digest=source.request_digest,
        created_at=now,
        updated_at=now,
        **values,
    )


def _request_digest(snapshot: BacktestTaskSnapshot) -> str:
    payload = snapshot.model_dump(mode="json", exclude={"id"})
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_execution_owner(item, execution_token: UUID) -> None:
    if item.execution_token != execution_token:
        raise _error("BACKTEST_EXECUTION_FENCED", "回测执行权已失效")


def _validate_frozen_test_snapshot(item, data: TrainingDataSnapshot) -> None:
    expected = (
        item.test_data_start_date,
        item.test_data_end_date,
        item.test_data_row_count,
        item.test_data_hash,
        item.test_price_basis,
    )
    actual = (
        data.start_date,
        data.end_date,
        len(data.rows),
        data.content_hash,
        data.price_basis,
    )
    if actual != expected:
        raise _error(
            BacktestErrorCode.TEST_DATA_INVALID.value,
            "恢复时测试数据必须与首次冻结快照一致",
        )


def _test_data_snapshot(item: BacktestItem) -> BacktestTestDataSnapshotView | None:
    values = (
        item.test_data_fetched_at,
        item.test_data_start_date,
        item.test_data_end_date,
        item.test_data_row_count,
        item.test_data_hash,
        item.test_price_basis,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise _error(
            BacktestErrorCode.TEST_DATA_INVALID.value,
            "测试数据冻结快照不完整",
        )
    return BacktestTestDataSnapshotView(
        item_id=item.id,
        fetched_at=item.test_data_fetched_at,
        start_date=item.test_data_start_date,
        end_date=item.test_data_end_date,
        row_count=item.test_data_row_count,
        data_hash=item.test_data_hash,
        price_basis=item.test_price_basis,
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
