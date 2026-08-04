from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from sqlalchemy import delete, func, select, update

from long_invest.modules.backtests.contracts import (
    BacktestAction,
    BacktestItemStatus,
    BacktestMode,
    BacktestPriceChangeRequest,
    BacktestPriceRollbackRequest,
    BacktestPriceVersionSource,
    BacktestPriceVersionView,
    BacktestTaskStatus,
    CandidateBacktestCreateRequest,
)
from long_invest.modules.backtests.engine import (
    BacktestBar,
    BacktestPricePoint,
    FixedTargetBacktestEngine,
)
from long_invest.modules.backtests.models import (
    BacktestControlCommand,
    BacktestDailyResult,
    BacktestForecastSnapshot,
    BacktestItem,
    BacktestMetric,
    BacktestOrder,
    BacktestPriceVersion,
    BacktestTargetAdjustment,
    BacktestTask,
    BacktestTrade,
    BacktestUniverseSnapshot,
)
from long_invest.modules.backtests.service import _result_models
from long_invest.modules.strategies.contracts import StrategyCandidateBatchSnapshot
from long_invest.modules.targets.contracts import TargetValues
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobProgress,
    JobResult,
    SubmitPostgresJob,
)
from long_invest.platform.jobs.postgres_service import PostgresJobService


class CandidateSnapshotPort(Protocol):
    async def candidate_snapshot(
        self, batch_id: UUID
    ) -> StrategyCandidateBatchSnapshot: ...


class CandidateStrategyPort(Protocol):
    async def get_execution_snapshot(self, strategy_version_id: UUID): ...


class CandidateDataPort(Protocol):
    async def get_training_data_from_dataset(self, **kwargs): ...


class CandidateBacktestApplication:
    def __init__(
        self,
        database: Any,
        *,
        candidates: CandidateSnapshotPort,
        strategies: CandidateStrategyPort,
        data: CandidateDataPort,
        engine: FixedTargetBacktestEngine,
        environment_version: str,
        runner_image_digest: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._candidates = candidates
        self._strategies = strategies
        self._data = data
        self._engine = engine
        self._environment_version = environment_version
        self._runner_image_digest = runner_image_digest
        self._clock = clock

    async def create(
        self,
        request: CandidateBacktestCreateRequest,
        *,
        request_id: str,
        actor_user_id: str,
    ) -> UUID:
        candidate = await self._candidates.candidate_snapshot(
            request.screening_batch_id
        )
        strategy = await self._strategies.get_execution_snapshot(
            candidate.strategy_version_id
        )
        if strategy is None or str(strategy.status) != "PUBLISHED":
            raise _error(
                "BACKTEST_STRATEGY_NOT_AVAILABLE",
                "候选批次使用的策略版本已不可用",
                422,
            )
        task_id = uuid5(
            UUID("36276874-9d76-52ba-9495-132c3c982f77"),
            request.idempotency_key,
        )
        digest = _hash(request.model_dump(mode="json", exclude={"idempotency_key"}))
        async with self._database.transaction() as session:
            existing = await session.scalar(
                select(BacktestTask).where(
                    BacktestTask.idempotency_key == request.idempotency_key
                )
            )
            if existing is not None:
                if existing.request_digest != digest:
                    raise _error(
                        "BACKTEST_IDEMPOTENCY_CONFLICT",
                        "幂等键已用于不同的回测请求",
                    )
                return existing.id
            representative_period = min(
                candidate.items, key=lambda item: item.period_sequence
            )
            securities = {
                item.security_id: {
                    "security_id": str(item.security_id),
                    "symbol": item.symbol,
                    "name": item.name,
                }
                for item in candidate.items
            }
            universe_values = sorted(
                securities.values(), key=lambda item: item["symbol"]
            )
            universe_hash = _hash(universe_values)
            now = self._clock()
            task = BacktestTask(
                id=task_id,
                mode=BacktestMode.MARKET.value,
                status=BacktestTaskStatus.PENDING.value,
                execution_generation=1,
                screening_batch_id=candidate.batch_id,
                idempotency_key=request.idempotency_key,
                request_digest=digest,
                universe_hash=universe_hash,
                training_start_date=representative_period.training_start_date,
                training_end_date=representative_period.training_end_date,
                test_start_date=representative_period.test_start_date,
                test_end_date=representative_period.test_end_date,
                strategy_version_id=candidate.strategy_version_id,
                source_code_hash=strategy.source_code_hash,
                strategy_metadata=dict(strategy.metadata),
                parameter_schema=dict(strategy.parameter_schema),
                parameter_snapshot=dict(candidate.parameter_snapshot),
                parameter_hash=candidate.parameter_hash,
                environment_version=strategy.environment_version,
                runner_image_digest=strategy.runner_image_digest,
                strategy_api_version=str(strategy.metadata.get("api_version", "1.0")),
                rule_version=self._engine.rule_version,
                hysteresis_ratio=Decimal("0.02"),
                minimum_hysteresis=Decimal("0.02"),
                price_basis="FROZEN_QFQ",
                data_source="FROZEN_DATASET",
                initial_capital=request.initial_capital,
                created_at=now,
                updated_at=now,
            )
            universe = BacktestUniverseSnapshot(
                task_id=task.id,
                scope_snapshot=universe_values,
                content_hash=universe_hash,
            )
            items = []
            forecasts = []
            prices = []
            for entry in candidate.items:
                item_id = uuid5(task.id, f"candidate:{entry.screening_result_id}")
                items.append(
                    BacktestItem(
                        id=item_id,
                        task_id=task.id,
                        security_id=entry.security_id,
                        screening_result_id=entry.screening_result_id,
                        screening_period_id=entry.screening_period_id,
                        status=BacktestItemStatus.PENDING.value,
                        price_version=1,
                    )
                )
                forecasts.append(
                    BacktestForecastSnapshot(
                        item_id=item_id,
                        training_start_date=entry.training_start_date,
                        training_end_date=entry.training_end_date,
                        training_row_count=entry.training_row_count,
                        training_fetched_at=now,
                        training_data_hash=entry.training_data_hash,
                        source_code_hash=strategy.source_code_hash,
                        parameter_hash=candidate.parameter_hash,
                        low_strong=entry.values.low_strong,
                        low_watch=entry.values.low_watch,
                        high_watch=entry.values.high_watch,
                        high_strong=entry.values.high_strong,
                        diagnostics={
                            "screening_batch_id": str(candidate.batch_id),
                            "screening_result_id": str(entry.screening_result_id),
                            "screening_period_id": str(entry.screening_period_id),
                            "period_sequence": entry.period_sequence,
                            "test_start_date": entry.test_start_date.isoformat(),
                            "test_end_date": entry.test_end_date.isoformat(),
                            "qfq_dataset_id": str(entry.qfq_dataset_id),
                            "qfq_data_version": entry.qfq_data_version,
                            "qfq_data_hash": entry.qfq_data_hash,
                            "symbol": entry.symbol,
                            "name": entry.name,
                        },
                        environment_version=strategy.environment_version,
                        runner_image_digest=strategy.runner_image_digest,
                        price_basis="FROZEN_QFQ",
                        frozen_at=now,
                    )
                )
                prices.append(
                    BacktestPriceVersion(
                        id=uuid5(item_id, "price:1"),
                        item_id=item_id,
                        version_no=1,
                        effective_date=entry.test_start_date,
                        low_strong=entry.values.low_strong,
                        low_watch=entry.values.low_watch,
                        high_watch=entry.values.high_watch,
                        high_strong=entry.values.high_strong,
                        source=BacktestPriceVersionSource.SCREENING.value,
                        reason="策略筛选初始四档价格",
                        actor_user_id=actor_user_id,
                        idempotency_key=f"screening:{entry.screening_result_id}",
                    )
                )
            session.add_all([task, universe, *items, *forecasts, *prices])
            await session.flush()
            job = await self._submit_job(
                session,
                task,
                request_id=request_id,
                actor_user_id=actor_user_id,
                concurrency=request.concurrency,
                item_ids=(),
                generation=1,
            )
            task.job_id = job.id
        return task_id

    async def run(self, context: JobExecutionContext) -> JobResult:
        try:
            task_id = UUID(str(context.config["task_id"]))
            concurrency = int(context.config.get("concurrency", 4))
            requested_ids = tuple(
                UUID(str(value)) for value in context.config.get("item_ids", ())
            )
            if not 1 <= concurrency <= 64:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return JobResult.failure(
                code="BACKTEST_CANDIDATE_CONFIG_INVALID",
                message="候选批次回测任务配置无效",
                retryable=False,
            )
        candidate, task = await self._prepare_run(task_id, requested_ids)
        by_result = {item.screening_result_id: item for item in candidate.items}
        while True:
            action = await self._task_action(task_id)
            if action:
                return await self._controlled_stop(context, task_id, action)
            items = await self._pending_items(task_id, requested_ids, concurrency)
            if not items:
                break
            await asyncio.gather(
                *(self._run_item(task, item, by_result) for item in items)
            )
            await self._progress(context, task_id, requested_ids)
        status, failed = await self._finish(task_id)
        data = {
            "task_id": str(task_id),
            "status": status.value,
            "failed_items": [str(value) for value in failed],
        }
        if status is BacktestTaskStatus.FAILED:
            return JobResult.failure(
                code="BACKTEST_CANDIDATE_FAILED",
                message="候选批次回测全部失败",
                retryable=False,
                data=data,
            )
        return JobResult(
            success=True,
            code="SUCCESS" if not failed else "PARTIAL",
            message="候选批次回测完成" if not failed else "候选批次回测部分完成",
            retryable=False,
            data=data,
        )

    async def control(
        self,
        task_id: UUID,
        action: BacktestAction,
        *,
        idempotency_key: str,
        reason: str,
    ) -> None:
        async with self._database.transaction() as session:
            task = await session.scalar(
                select(BacktestTask).where(BacktestTask.id == task_id).with_for_update()
            )
            if task is None or task.screening_batch_id is None or task.job_id is None:
                raise _not_found()
            digest = _hash(
                {"task_id": str(task_id), "action": action.value, "reason": reason}
            )
            existing = await session.scalar(
                select(BacktestControlCommand).where(
                    BacktestControlCommand.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if (
                    existing.task_id != task_id
                    or existing.action != action.value
                    or existing.request_digest != digest
                ):
                    raise _error(
                        "BACKTEST_IDEMPOTENCY_CONFLICT",
                        "幂等键已用于不同的回测控制请求",
                    )
                return
            status = BacktestTaskStatus(task.status)
            jobs = PostgresJobService(session)
            if action is BacktestAction.PAUSE and status in {
                BacktestTaskStatus.PENDING,
                BacktestTaskStatus.RUNNING,
            }:
                await jobs.command(task.job_id, "pause")
                task.status = (
                    BacktestTaskStatus.PAUSED.value
                    if status is BacktestTaskStatus.PENDING
                    else BacktestTaskStatus.PAUSING.value
                )
            elif (
                action is BacktestAction.RESUME and status is BacktestTaskStatus.PAUSED
            ):
                await jobs.command(task.job_id, "resume")
                task.status = BacktestTaskStatus.PENDING.value
            elif action is BacktestAction.CANCEL and status in {
                BacktestTaskStatus.PENDING,
                BacktestTaskStatus.RUNNING,
                BacktestTaskStatus.PAUSING,
                BacktestTaskStatus.PAUSED,
            }:
                await jobs.command(task.job_id, "cancel")
                task.status = (
                    BacktestTaskStatus.CANCELED.value
                    if status in {BacktestTaskStatus.PENDING, BacktestTaskStatus.PAUSED}
                    else BacktestTaskStatus.CANCELING.value
                )
                if task.status == BacktestTaskStatus.CANCELED.value:
                    now = self._clock()
                    await session.execute(
                        update(BacktestItem)
                        .where(
                            BacktestItem.task_id == task.id,
                            BacktestItem.status == BacktestItemStatus.PENDING.value,
                        )
                        .values(
                            status=BacktestItemStatus.CANCELED.value,
                            failure_code=None,
                            ended_at=now,
                        )
                    )
                    task.terminal_at = now
            elif action is BacktestAction.RETRY_FAILED and status in {
                BacktestTaskStatus.PARTIAL,
                BacktestTaskStatus.FAILED,
            }:
                failed = list(
                    await session.scalars(
                        select(BacktestItem).where(
                            BacktestItem.task_id == task.id,
                            BacktestItem.status == BacktestItemStatus.FAILED.value,
                        )
                    )
                )
                if not failed:
                    raise _state_conflict()
                for item in failed:
                    item.status = BacktestItemStatus.PENDING.value
                    item.failure_code = None
                    item.started_at = None
                    item.ended_at = None
                job = await self._submit_job(
                    session,
                    task,
                    request_id=f"retry:{task.id}:{task.execution_generation + 1}",
                    actor_user_id="system:retry",
                    concurrency=4,
                    item_ids=tuple(item.id for item in failed),
                    generation=task.execution_generation + 1,
                )
                task.job_id = job.id
                task.execution_generation += 1
                task.status = BacktestTaskStatus.PENDING.value
                task.terminal_at = None
            else:
                raise _state_conflict()
            session.add(
                BacktestControlCommand(
                    task_id=task.id,
                    action=action.value,
                    idempotency_key=idempotency_key,
                    request_digest=digest,
                )
            )
            task.updated_at = self._clock()

    async def change_prices(
        self,
        item_id: UUID,
        request: BacktestPriceChangeRequest,
        *,
        actor_user_id: str,
        request_id: str,
    ) -> BacktestPriceVersionView:
        return await self._new_price_version(
            item_id,
            effective_date=request.effective_date,
            values=request.values,
            source=BacktestPriceVersionSource.USER,
            source_version_id=None,
            reason=request.reason,
            expected_version=request.expected_version,
            idempotency_key=request.idempotency_key,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )

    async def rollback_prices(
        self,
        item_id: UUID,
        request: BacktestPriceRollbackRequest,
        *,
        actor_user_id: str,
        request_id: str,
    ) -> BacktestPriceVersionView:
        async with self._database.session() as session:
            source = await session.get(BacktestPriceVersion, request.source_version_id)
            if source is None or source.item_id != item_id:
                raise _error(
                    "BACKTEST_PRICE_VERSION_NOT_FOUND", "选择的价格版本不存在", 404
                )
            values = _price_values(source)
        return await self._new_price_version(
            item_id,
            effective_date=request.effective_date,
            values=values,
            source=BacktestPriceVersionSource.ROLLBACK,
            source_version_id=source.id,
            reason=request.reason,
            expected_version=request.expected_version,
            idempotency_key=request.idempotency_key,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )

    async def list_price_versions(
        self, item_id: UUID
    ) -> tuple[BacktestPriceVersionView, ...]:
        async with self._database.session() as session:
            rows = await session.scalars(
                select(BacktestPriceVersion)
                .where(BacktestPriceVersion.item_id == item_id)
                .order_by(BacktestPriceVersion.version_no.desc())
            )
            return tuple(_price_view(row) for row in rows)

    async def _new_price_version(
        self,
        item_id: UUID,
        *,
        effective_date: date,
        values: TargetValues,
        source: BacktestPriceVersionSource,
        source_version_id: UUID | None,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        actor_user_id: str,
        request_id: str,
    ) -> BacktestPriceVersionView:
        async with self._database.transaction() as session:
            existing = await session.scalar(
                select(BacktestPriceVersion).where(
                    BacktestPriceVersion.item_id == item_id,
                    BacktestPriceVersion.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                same_values = _price_values(existing) == values
                same_request = (
                    existing.effective_date == effective_date
                    and same_values
                    and existing.source == source.value
                    and existing.source_version_id == source_version_id
                    and existing.reason == reason
                    and existing.actor_user_id == actor_user_id
                )
                if not same_request:
                    raise _error(
                        "BACKTEST_IDEMPOTENCY_CONFLICT",
                        "幂等键已用于不同的价格修改请求",
                    )
                return _price_view(existing)
            row = await session.execute(
                select(BacktestItem, BacktestTask, BacktestForecastSnapshot)
                .join(BacktestTask, BacktestTask.id == BacktestItem.task_id)
                .join(
                    BacktestForecastSnapshot,
                    BacktestForecastSnapshot.item_id == BacktestItem.id,
                )
                .where(BacktestItem.id == item_id)
                .with_for_update()
            )
            found = row.one_or_none()
            if found is None:
                raise _not_found()
            item, task, forecast = found
            if task.screening_batch_id is None:
                raise _error(
                    "BACKTEST_PRICE_VERSION_NOT_SUPPORTED",
                    "旧单时段回测不支持价格版本修改",
                    422,
                )
            if item.price_version != expected_version:
                raise _error(
                    "BACKTEST_PRICE_VERSION_CONFLICT",
                    "价格版本已变化，请刷新后重试",
                )
            test_start = date.fromisoformat(forecast.diagnostics["test_start_date"])
            test_end = date.fromisoformat(forecast.diagnostics["test_end_date"])
            if not test_start <= effective_date <= test_end:
                raise _error(
                    "BACKTEST_PRICE_EFFECTIVE_DATE_INVALID",
                    "生效日期必须位于该时段的测试期内",
                    422,
                )
            version_no = item.price_version + 1
            version = BacktestPriceVersion(
                id=uuid5(item.id, f"price:{version_no}:{idempotency_key}"),
                item_id=item.id,
                version_no=version_no,
                effective_date=effective_date,
                low_strong=values.low_strong,
                low_watch=values.low_watch,
                high_watch=values.high_watch,
                high_strong=values.high_strong,
                source=source.value,
                reason=reason,
                actor_user_id=actor_user_id,
                source_version_id=source_version_id,
                idempotency_key=idempotency_key,
            )
            session.add(version)
            item.price_version = version_no
            item.recompute_from_date = (
                min(item.recompute_from_date, effective_date)
                if item.recompute_from_date
                else effective_date
            )
            item.status = BacktestItemStatus.PENDING.value
            item.failure_code = None
            item.ended_at = None
            task.status = BacktestTaskStatus.PENDING.value
            task.execution_generation += 1
            task.terminal_at = None
            task.updated_at = self._clock()
            job = await self._submit_job(
                session,
                task,
                request_id=request_id,
                actor_user_id=actor_user_id,
                concurrency=1,
                item_ids=(item.id,),
                generation=task.execution_generation,
            )
            task.job_id = job.id
            await session.flush()
            return _price_view(version)

    async def _prepare_run(
        self, task_id: UUID, requested_ids: tuple[UUID, ...]
    ) -> tuple[StrategyCandidateBatchSnapshot, BacktestTask]:
        async with self._database.transaction() as session:
            task = await session.scalar(
                select(BacktestTask).where(BacktestTask.id == task_id).with_for_update()
            )
            if task is None or task.screening_batch_id is None:
                raise _not_found()
            if (
                task.status
                in {
                    BacktestTaskStatus.SUCCEEDED.value,
                    BacktestTaskStatus.CANCELED.value,
                }
                and not requested_ids
            ):
                return (
                    await self._candidates.candidate_snapshot(task.screening_batch_id),
                    task,
                )
            await session.execute(
                update(BacktestItem)
                .where(
                    BacktestItem.task_id == task.id,
                    BacktestItem.status.in_(
                        (
                            BacktestItemStatus.FETCHING_DATA.value,
                            BacktestItemStatus.VALIDATING_DATA.value,
                            BacktestItemStatus.SIMULATING.value,
                            BacktestItemStatus.SAVING.value,
                        )
                    ),
                )
                .values(status=BacktestItemStatus.PENDING.value)
            )
            task.status = BacktestTaskStatus.RUNNING.value
            task.updated_at = self._clock()
        return await self._candidates.candidate_snapshot(task.screening_batch_id), task

    async def _task_action(self, task_id: UUID) -> str | None:
        async with self._database.session() as session:
            status = await session.scalar(
                select(BacktestTask.status).where(BacktestTask.id == task_id)
            )
            if status == BacktestTaskStatus.PAUSING.value:
                return "PAUSE"
            if status == BacktestTaskStatus.CANCELING.value:
                return "CANCEL"
            return None

    async def _pending_items(
        self,
        task_id: UUID,
        requested_ids: tuple[UUID, ...],
        limit: int,
    ) -> list[BacktestItem]:
        conditions = [
            BacktestItem.task_id == task_id,
            BacktestItem.status == BacktestItemStatus.PENDING.value,
        ]
        if requested_ids:
            conditions.append(BacktestItem.id.in_(requested_ids))
        async with self._database.session() as session:
            rows = await session.scalars(
                select(BacktestItem)
                .where(*conditions)
                .order_by(BacktestItem.id)
                .limit(limit)
            )
            return list(rows)

    async def _run_item(self, task, item, by_result) -> None:
        candidate = by_result.get(item.screening_result_id)
        if candidate is None:
            await self._fail_item(item.id, "BACKTEST_CANDIDATE_CHANGED")
            return
        now = self._clock()
        token = uuid4()
        async with self._database.transaction() as session:
            claimed = await session.scalar(
                update(BacktestItem)
                .where(
                    BacktestItem.id == item.id,
                    BacktestItem.status == BacktestItemStatus.PENDING.value,
                )
                .values(
                    status=BacktestItemStatus.FETCHING_DATA.value,
                    execution_token=token,
                    attempt_count=BacktestItem.attempt_count + 1,
                    started_at=now,
                )
                .returning(BacktestItem.id)
            )
        if claimed is None:
            return
        try:
            data = await self._data.get_training_data_from_dataset(
                dataset_id=candidate.qfq_dataset_id,
                security_id=candidate.security_id,
                start_date=candidate.test_start_date,
                end_date=candidate.test_end_date,
            )
            if data is None:
                raise ValueError("test data unavailable")
            versions = await self._price_points(item.id)
            result = self._engine.run(
                item_id=item.id,
                security_id=item.security_id,
                bars=tuple(
                    BacktestBar(
                        trade_date=row["trade_date"],
                        open_price=Decimal(str(row["open"])),
                        close_price=Decimal(str(row["close"])),
                    )
                    for row in data.rows
                ),
                targets=candidate.values,
                adjustments=(),
                initial_capital=task.initial_capital,
                hysteresis_ratio=task.hysteresis_ratio,
                minimum_hysteresis=task.minimum_hysteresis,
                price_versions=versions,
            )
        except Exception:
            await self._fail_item(item.id, "BACKTEST_SIMULATION_FAILED")
            return
        async with self._database.transaction() as session:
            locked = await session.scalar(
                select(BacktestItem).where(BacktestItem.id == item.id).with_for_update()
            )
            if locked is None or locked.execution_token != token:
                return
            await self._delete_results(session, item.id)
            models = _result_models(result)
            session.add_all(
                [
                    *models["adjustments"],
                    *models["orders"],
                    *models["trades"],
                    *models["daily_results"],
                    models["metric"],
                ]
            )
            locked.status = BacktestItemStatus.SUCCEEDED.value
            locked.failure_code = None
            locked.execution_token = None
            locked.test_data_fetched_at = data.fetched_at
            locked.test_data_start_date = data.start_date
            locked.test_data_end_date = data.end_date
            locked.test_data_row_count = len(data.rows)
            locked.test_data_hash = data.content_hash
            locked.test_price_basis = data.price_basis
            locked.recompute_from_date = None
            locked.ended_at = self._clock()

    async def _price_points(self, item_id: UUID) -> tuple[BacktestPricePoint, ...]:
        async with self._database.session() as session:
            rows = list(
                await session.scalars(
                    select(BacktestPriceVersion)
                    .where(BacktestPriceVersion.item_id == item_id)
                    .order_by(
                        BacktestPriceVersion.effective_date,
                        BacktestPriceVersion.version_no.desc(),
                    )
                )
            )
        by_date = {}
        for row in rows:
            by_date.setdefault(row.effective_date, row)
        return tuple(
            BacktestPricePoint(
                effective_date=value.effective_date, values=_price_values(value)
            )
            for value in by_date.values()
        )

    async def _fail_item(self, item_id: UUID, code: str) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                update(BacktestItem)
                .where(BacktestItem.id == item_id)
                .values(
                    status=BacktestItemStatus.FAILED.value,
                    failure_code=code,
                    execution_token=None,
                    ended_at=self._clock(),
                )
            )

    @staticmethod
    async def _delete_results(session, item_id: UUID) -> None:
        await session.execute(
            delete(BacktestDailyResult).where(BacktestDailyResult.item_id == item_id)
        )
        await session.execute(
            delete(BacktestTrade).where(BacktestTrade.item_id == item_id)
        )
        await session.execute(
            delete(BacktestOrder).where(BacktestOrder.item_id == item_id)
        )
        await session.execute(
            delete(BacktestTargetAdjustment).where(
                BacktestTargetAdjustment.item_id == item_id
            )
        )
        await session.execute(
            delete(BacktestMetric).where(BacktestMetric.item_id == item_id)
        )

    async def _progress(self, context, task_id, requested_ids) -> None:
        async with self._database.transaction() as session:
            condition = BacktestItem.task_id == task_id
            if requested_ids:
                condition = condition & BacktestItem.id.in_(requested_ids)
            counts = dict(
                await session.execute(
                    select(BacktestItem.status, func.count())
                    .where(condition)
                    .group_by(BacktestItem.status)
                )
            )
            total = sum(int(value) for value in counts.values())
            completed = sum(
                int(counts.get(status.value, 0))
                for status in (
                    BacktestItemStatus.SUCCEEDED,
                    BacktestItemStatus.FAILED,
                    BacktestItemStatus.CANCELED,
                )
            )
            await PostgresJobService(session).report_progress(
                context.job_id,
                context.fence_token,
                progress=JobProgress(completed=completed, total=total),
                checkpoint={},
                lease_duration=timedelta(seconds=60),
            )

    async def _finish(self, task_id: UUID) -> tuple[BacktestTaskStatus, list[UUID]]:
        async with self._database.transaction() as session:
            task = await session.scalar(
                select(BacktestTask).where(BacktestTask.id == task_id).with_for_update()
            )
            if task is None:
                raise _not_found()
            failed = list(
                await session.scalars(
                    select(BacktestItem.id).where(
                        BacktestItem.task_id == task_id,
                        BacktestItem.status == BacktestItemStatus.FAILED.value,
                    )
                )
            )
            succeeded = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BacktestItem)
                    .where(
                        BacktestItem.task_id == task_id,
                        BacktestItem.status == BacktestItemStatus.SUCCEEDED.value,
                    )
                )
                or 0
            )
            status = (
                BacktestTaskStatus.SUCCEEDED
                if not failed
                else BacktestTaskStatus.PARTIAL
                if succeeded
                else BacktestTaskStatus.FAILED
            )
            task.status = status.value
            task.updated_at = self._clock()
            task.terminal_at = task.updated_at
            return status, failed

    async def _controlled_stop(self, context, task_id, action) -> JobResult:
        async with self._database.transaction() as session:
            task = await session.scalar(
                select(BacktestTask).where(BacktestTask.id == task_id).with_for_update()
            )
            if task is None:
                raise _not_found()
            if action == "PAUSE":
                task.status = BacktestTaskStatus.PAUSED.value
                await PostgresJobService(session).report_progress(
                    context.job_id,
                    context.fence_token,
                    progress=JobProgress(completed=0, total=1),
                    checkpoint={},
                    lease_duration=timedelta(seconds=60),
                    pause=True,
                )
            else:
                await session.execute(
                    update(BacktestItem)
                    .where(
                        BacktestItem.task_id == task_id,
                        BacktestItem.status == BacktestItemStatus.PENDING.value,
                    )
                    .values(
                        status=BacktestItemStatus.CANCELED.value, ended_at=self._clock()
                    )
                )
                task.status = BacktestTaskStatus.CANCELED.value
                task.terminal_at = self._clock()
        return JobResult.success_result(
            data={"task_id": str(task_id), "status": task.status}
        )

    async def _submit_job(
        self,
        session,
        task,
        *,
        request_id,
        actor_user_id,
        concurrency,
        item_ids,
        generation,
    ):
        item_key = ",".join(map(str, item_ids))
        return await PostgresJobService(session).submit(
            SubmitPostgresJob(
                job_type="BACKTEST_CANDIDATE_BATCH",
                module_owner="backtests",
                idempotency_scope="candidate-backtest-run",
                idempotency_key=f"{task.id}:{generation}:{item_key}",
                request_id=request_id,
                config_snapshot={
                    "task_id": str(task.id),
                    "concurrency": concurrency,
                    "item_ids": [str(value) for value in item_ids],
                },
                priority=2,
                business_object_type="backtest_task",
                business_object_id=str(task.id),
                created_by_user_id=actor_user_id,
                soft_timeout_seconds=82800,
                hard_timeout_seconds=86400,
                recoverable=True,
                max_recoveries=20,
            )
        )


def _price_values(row) -> TargetValues:
    return TargetValues(
        low_strong=row.low_strong,
        low_watch=row.low_watch,
        high_watch=row.high_watch,
        high_strong=row.high_strong,
    )


def _price_view(row) -> BacktestPriceVersionView:
    return BacktestPriceVersionView(
        id=row.id,
        item_id=row.item_id,
        version_no=row.version_no,
        effective_date=row.effective_date,
        values=_price_values(row),
        source=row.source,
        reason=row.reason,
        actor_user_id=row.actor_user_id,
        source_version_id=row.source_version_id,
        created_at=row.created_at,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _error(code: str, message: str, status_code: int = 409) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)


def _not_found() -> AppError:
    return _error("BACKTEST_NOT_FOUND", "回测不存在", 404)


def _state_conflict() -> AppError:
    return _error("BACKTEST_STATE_CONFLICT", "回测状态不允许当前操作")
