from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from long_invest.modules.backtests.contracts import (
    BacktestCreateRequest,
    BacktestCreationSnapshotPort,
    BacktestErrorCode,
    BacktestItemStatus,
    BacktestStrategyExecutionPort,
)
from long_invest.modules.backtests.engine import BacktestBar, FixedTargetBacktestEngine
from long_invest.modules.backtests.repository import BacktestRepository
from long_invest.modules.backtests.service import (
    BacktestCommandContext,
    BacktestService,
)
from long_invest.modules.market_data.contracts import AdjustmentTimelinePort
from long_invest.modules.strategies.contracts import (
    StrategyForecastPort,
    StrategyForecastRequest,
    TestDataPort,
    TrainingDataPort,
)
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import JobExecutionContext, JobResult

_TERMINAL_ITEM_STATUSES = {
    BacktestItemStatus.SUCCEEDED,
    BacktestItemStatus.FAILED,
    BacktestItemStatus.SKIPPED,
    BacktestItemStatus.CANCELED,
}
_NON_FATAL_CONFLICTS = {
    "BACKTEST_ALREADY_RUNNING",
    "BACKTEST_EXECUTION_FENCED",
}


class BacktestApplication:
    def __init__(
        self,
        database: Database,
        *,
        creation_snapshots: BacktestCreationSnapshotPort,
        strategy_executions: BacktestStrategyExecutionPort,
        training_data: TrainingDataPort,
        test_data: TestDataPort,
        forecasts: StrategyForecastPort,
        adjustments: AdjustmentTimelinePort,
        engine: FixedTargetBacktestEngine,
        repository_factory: Callable[..., Any] = BacktestRepository,
        service_factory: Callable[..., Any] = BacktestService,
        event_factory: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._creation_snapshots = creation_snapshots
        self._strategy_executions = strategy_executions
        self._training_data = training_data
        self._test_data = test_data
        self._forecasts = forecasts
        self._adjustments = adjustments
        self._engine = engine
        self._repository_factory = repository_factory
        self._service_factory = service_factory
        self._event_factory = event_factory
        self._clock = clock

    async def create(
        self,
        *,
        task_id: UUID,
        request: BacktestCreateRequest,
        context: BacktestCommandContext,
    ):
        snapshot = await self._creation_snapshots.resolve_creation_snapshot(
            task_id=task_id, request=request
        )
        try:
            return await self._write("create", snapshot, context)
        except AppError as exc:
            if exc.code != "BACKTEST_CONFLICT":
                raise
            return await self._write("create", snapshot, context)

    async def get_execution(self, task_id: UUID):
        return await self._read("get_execution", task_id)

    async def get_result(self, task_id: UUID, item_id: UUID):
        return await self._read("get_result", task_id, item_id)

    async def run(self, task_id: UUID, *, execution_token: UUID):
        try:
            state = await self._write("start", task_id, execution_token)
            if state.item_status in _TERMINAL_ITEM_STATUSES:
                return state
            task = state.task
            if self._engine.rule_version != task.rule_version:
                raise _failure(
                    "BACKTEST_RULE_VERSION_MISMATCH",
                    "回测规则版本与冻结任务不一致",
                )
            entry = task.universe_snapshot[0]
            forecast = state.forecast
            if forecast is None:
                training = await self._training_data.get_training_data(
                    security_id=entry.security_id,
                    start_date=task.date_range.training_start_date,
                    end_date=task.date_range.training_end_date,
                )
                if training is None:
                    raise _failure(
                        BacktestErrorCode.INSUFFICIENT_HISTORY,
                        "训练期历史数据不足",
                    )
                _verify_security_snapshot(
                    entry, training, BacktestErrorCode.TRAINING_DATA_INVALID
                )
                claimed = await self._write(
                    "claim_forecast",
                    task_id,
                    training,
                    execution_token=execution_token,
                )
                forecast = claimed.forecast
                if forecast is None:
                    execution = await self._strategy_executions.resolve_execution(task)
                    request = StrategyForecastRequest(
                        strategy_id=execution.strategy_id,
                        security_name=entry.name,
                        strategy_version_id=task.strategy_version_id,
                        draft_id=task.draft_id,
                        draft_version=task.draft_version,
                        source_code=execution.source_code,
                        source_code_hash=task.source_code_hash,
                        metadata=task.strategy_metadata,
                        parameter_schema=task.parameter_schema,
                        environment_version=task.environment_version,
                        runner_image_digest=task.runner_image_digest,
                        parameter_snapshot=task.parameter_snapshot,
                        parameter_hash=task.parameter_hash,
                        training_data=training,
                        requested_at=self._clock(),
                    )
                    result = await self._forecasts.forecast(request)
                    forecast = await self._write(
                        "freeze_forecast",
                        task_id,
                        training,
                        result,
                        execution_token=execution_token,
                        frozen_at=self._clock(),
                    )
                    del request, result, execution
                del training

            test_data = await self._test_data.get_test_data(
                security_id=entry.security_id,
                start_date=task.date_range.test_start_date,
                end_date=task.date_range.test_end_date,
            )
            if test_data is None:
                raise _failure(
                    BacktestErrorCode.TEST_DATA_INVALID, "测试期没有有效行情"
                )
            _verify_security_snapshot(
                entry, test_data, BacktestErrorCode.TEST_DATA_INVALID
            )
            await self._write(
                "claim_simulation",
                task_id,
                test_data,
                execution_token=execution_token,
            )
            timeline = state.adjustment_snapshot
            if timeline is None:
                timeline = await self._adjustments.get_adjustment_timeline(
                    security_id=entry.security_id,
                    start_date=task.date_range.training_end_date
                    + timedelta(days=1),
                    end_date=task.date_range.test_end_date,
                    as_of=self._clock(),
                )
                timeline = await self._write(
                    "freeze_adjustment_snapshot",
                    task_id,
                    timeline,
                    execution_token=execution_token,
                )
            result = self._engine.run(
                item_id=state.item_id,
                security_id=entry.security_id,
                bars=tuple(_bar(row) for row in test_data.rows),
                targets=forecast.values,
                adjustments=timeline.entries,
                initial_capital=task.initial_capital,
                hysteresis_ratio=task.hysteresis_ratio,
                minimum_hysteresis=task.minimum_hysteresis,
            )
            return await self._write(
                "save_success",
                task_id,
                test_data,
                result,
                execution_token=execution_token,
            )
        except AppError as exc:
            if exc.code not in _NON_FATAL_CONFLICTS:
                await self._record_failure(task_id, exc.code, execution_token)
            raise
        except RuntimeError as exc:
            code = _forecast_failure_code(exc)
            await self._record_failure(task_id, code, execution_token)
            raise _failure(code, "策略预测失败") from exc
        except TimeoutError as exc:
            code = BacktestErrorCode.STRATEGY_FORECAST_TIMEOUT.value
            await self._record_failure(task_id, code, execution_token)
            raise _failure(code, "策略预测超时") from exc
        except (SQLAlchemyError, ValueError) as exc:
            code = BacktestErrorCode.BACKTEST_RESULT_SAVE_FAILED.value
            await self._record_failure(task_id, code, execution_token)
            raise _failure(code, "回测执行或结果保存失败") from exc

    async def recover(self, task_id: UUID, *, execution_token: UUID):
        await self._write("recover", task_id, execution_token=execution_token)
        return await self.run(task_id, execution_token=execution_token)

    async def _record_failure(
        self, task_id: UUID, code: str, execution_token: UUID
    ) -> None:
        try:
            await self._write(
                "fail", task_id, code, execution_token=execution_token
            )
        except (AppError, SQLAlchemyError):
            return

    async def _read(self, method: str, *args: Any, **kwargs: Any):
        async with self._database.session() as session:
            return await getattr(self._service(session), method)(*args, **kwargs)

    async def _write(self, method: str, *args: Any, **kwargs: Any):
        try:
            async with self._database.transaction() as session:
                return await getattr(self._service(session), method)(*args, **kwargs)
        except IntegrityError as exc:
            raise _failure("BACKTEST_CONFLICT", "回测请求与已有操作冲突") from exc

    def _service(self, session: Any):
        events = self._event_factory(session) if self._event_factory else None
        return self._service_factory(self._repository_factory(session), events=events)


def _verify_security_snapshot(entry, data, code: BacktestErrorCode) -> None:
    if data.security_id != entry.security_id or data.symbol != entry.symbol:
        raise _failure(code, "行情数据与冻结股票不一致")


def _bar(row) -> BacktestBar:
    return BacktestBar(
        trade_date=row["trade_date"],
        open_price=Decimal(str(row["open"])),
        close_price=Decimal(str(row["close"])),
    )


def _forecast_failure_code(exc: RuntimeError) -> str:
    value = str(getattr(exc, "code", "STRATEGY_FORECAST_FAILED"))
    if "TIMEOUT" in value:
        return BacktestErrorCode.STRATEGY_FORECAST_TIMEOUT.value
    if "TARGET" in value or "RESULT" in value:
        return BacktestErrorCode.STRATEGY_TARGET_INVALID.value
    return value


def _failure(code: BacktestErrorCode | str, message: str) -> AppError:
    value = code.value if isinstance(code, BacktestErrorCode) else code
    return AppError(code=value, message=message, status_code=409)


def build_backtest_job_handler(application: BacktestApplication):
    async def handle(context: JobExecutionContext) -> JobResult:
        try:
            task_id = UUID(str(context.config["backtest_task_id"]))
        except (KeyError, TypeError, ValueError):
            return JobResult.failure(
                code="BACKTEST_JOB_CONFIG_INVALID",
                message="回测任务配置无效",
                retryable=False,
            )
        try:
            if context.config.get("recover") is True:
                state = await application.recover(
                    task_id, execution_token=context.job_id
                )
            else:
                state = await application.run(task_id, execution_token=context.job_id)
        except AppError as exc:
            return JobResult.failure(
                code=exc.code,
                message=exc.message,
                retryable=exc.code in _NON_FATAL_CONFLICTS,
            )
        return JobResult.success_result(
            data={"backtest_task_id": str(task_id), "status": state.item_status.value}
        )

    return handle
