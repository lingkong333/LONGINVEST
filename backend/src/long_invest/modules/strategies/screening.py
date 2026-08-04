from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid5

from sqlalchemy import func, select, update

from long_invest.modules.qfq.contracts import QfqFreshness
from long_invest.modules.strategies.contracts import (
    StrategyCandidateBatchSnapshot,
    StrategyCandidateItem,
    StrategyForecastRequest,
    StrategyScreeningAction,
    StrategyScreeningBatchPage,
    StrategyScreeningBatchStatus,
    StrategyScreeningBatchView,
    StrategyScreeningCreateRequest,
    StrategyScreeningErrorCode,
    StrategyScreeningResultPage,
    StrategyScreeningResultStatus,
    StrategyScreeningResultView,
    TrainingDataSnapshot,
)
from long_invest.modules.strategies.contracts import (
    StrategyScreeningPeriod as ScreeningPeriodContract,
)
from long_invest.modules.strategies.forecast import StrategyForecastFailure
from long_invest.modules.strategies.models import (
    StrategyScreeningBatch,
    StrategyScreeningControlCommand,
    StrategyScreeningPeriod,
    StrategyScreeningResult,
    StrategyScreeningScopeItem,
)
from long_invest.modules.strategies.runner_client import StrategyRunnerFailure
from long_invest.modules.targets.contracts import TargetValues
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobProgress,
    JobResult,
    SubmitPostgresJob,
)
from long_invest.platform.jobs.postgres_service import PostgresJobService

_TERMINAL_BATCH_STATUSES = {
    StrategyScreeningBatchStatus.SUCCEEDED,
    StrategyScreeningBatchStatus.CANCELED,
}
_FINAL_RESULT_STATUSES = {
    StrategyScreeningResultStatus.MATCHED.value,
    StrategyScreeningResultStatus.NOT_MATCHED.value,
    StrategyScreeningResultStatus.FAILED.value,
    StrategyScreeningResultStatus.CANCELED.value,
}


class ScreeningStrategyPort(Protocol):
    async def get_execution_snapshot(self, strategy_version_id: UUID): ...


class ScreeningScopePort(Protocol):
    async def list_market(self) -> tuple[Any, ...]: ...

    async def freeze_in_transaction(self, session: Any): ...


class ScreeningDatasetPort(Protocol):
    async def current_dataset_snapshots(
        self, security_ids: tuple[UUID, ...]
    ) -> tuple[Any, ...]: ...

    async def get_training_data_from_dataset(
        self,
        *,
        dataset_id: UUID,
        security_id: UUID,
        start_date,
        end_date,
    ) -> TrainingDataSnapshot | None: ...


class ScreeningForecastPort(Protocol):
    async def forecast(self, request: StrategyForecastRequest): ...


class ScreeningRepository:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def get_batch(
        self, batch_id: UUID, *, for_update: bool = False
    ) -> StrategyScreeningBatch | None:
        statement = select(StrategyScreeningBatch).where(
            StrategyScreeningBatch.id == batch_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def get_by_idempotency(
        self, idempotency_key: str
    ) -> StrategyScreeningBatch | None:
        return await self.session.scalar(
            select(StrategyScreeningBatch).where(
                StrategyScreeningBatch.idempotency_key == idempotency_key
            )
        )

    async def list_batches(self, *, page: int, page_size: int):
        statement = select(StrategyScreeningBatch).order_by(
            StrategyScreeningBatch.created_at.desc(),
            StrategyScreeningBatch.id.desc(),
        )
        rows = await self.session.scalars(
            statement.offset((page - 1) * page_size).limit(page_size)
        )
        total = await self.session.scalar(
            select(func.count(StrategyScreeningBatch.id))
        )
        return list(rows), int(total or 0)

    async def periods(self, batch_id: UUID) -> list[StrategyScreeningPeriod]:
        rows = await self.session.scalars(
            select(StrategyScreeningPeriod)
            .where(StrategyScreeningPeriod.batch_id == batch_id)
            .order_by(StrategyScreeningPeriod.sequence_no)
        )
        return list(rows)

    async def counts(self, batch_id: UUID) -> dict[str, int]:
        rows = await self.session.execute(
            select(StrategyScreeningResult.status, func.count())
            .where(StrategyScreeningResult.batch_id == batch_id)
            .group_by(StrategyScreeningResult.status)
        )
        return {str(status): int(count) for status, count in rows}

    async def list_results(
        self,
        batch_id: UUID,
        *,
        page: int,
        page_size: int,
        symbol: str | None,
        period_id: UUID | None,
        status: StrategyScreeningResultStatus | None,
    ) -> tuple[list[tuple[Any, ...]], int]:
        conditions = [StrategyScreeningResult.batch_id == batch_id]
        if symbol:
            conditions.append(StrategyScreeningScopeItem.symbol == symbol)
        if period_id:
            conditions.append(StrategyScreeningResult.period_id == period_id)
        if status:
            conditions.append(StrategyScreeningResult.status == status.value)
        base = (
            select(
                StrategyScreeningResult,
                StrategyScreeningScopeItem,
                StrategyScreeningPeriod,
            )
            .join(
                StrategyScreeningScopeItem,
                StrategyScreeningScopeItem.id
                == StrategyScreeningResult.scope_item_id,
            )
            .join(
                StrategyScreeningPeriod,
                StrategyScreeningPeriod.id == StrategyScreeningResult.period_id,
            )
            .where(*conditions)
        )
        total = int(
            await self.session.scalar(
                select(func.count()).select_from(base.subquery())
            )
            or 0
        )
        rows = await self.session.execute(
            base.order_by(
                StrategyScreeningScopeItem.symbol,
                StrategyScreeningPeriod.sequence_no,
                StrategyScreeningResult.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.tuples()), total

    async def work_items(
        self, batch_id: UUID, *, limit: int
    ) -> list[tuple[Any, ...]]:
        rows = await self.session.execute(
            select(
                StrategyScreeningResult,
                StrategyScreeningScopeItem,
                StrategyScreeningPeriod,
            )
            .join(
                StrategyScreeningScopeItem,
                StrategyScreeningScopeItem.id
                == StrategyScreeningResult.scope_item_id,
            )
            .join(
                StrategyScreeningPeriod,
                StrategyScreeningPeriod.id == StrategyScreeningResult.period_id,
            )
            .where(
                StrategyScreeningResult.batch_id == batch_id,
                StrategyScreeningResult.status
                == StrategyScreeningResultStatus.PENDING.value,
            )
            .order_by(
                StrategyScreeningScopeItem.symbol,
                StrategyScreeningPeriod.sequence_no,
            )
            .limit(limit)
        )
        return list(rows.tuples())

    async def claim_result(self, result_id: UUID, now: datetime) -> bool:
        claimed = await self.session.scalar(
            update(StrategyScreeningResult)
            .where(
                StrategyScreeningResult.id == result_id,
                StrategyScreeningResult.status
                == StrategyScreeningResultStatus.PENDING.value,
            )
            .values(
                status=StrategyScreeningResultStatus.RUNNING.value,
                attempt_count=StrategyScreeningResult.attempt_count + 1,
                started_at=now,
                ended_at=None,
            )
            .returning(StrategyScreeningResult.id)
        )
        return claimed is not None

    async def recover_inflight(self, batch_id: UUID) -> None:
        await self.session.execute(
            update(StrategyScreeningResult)
            .where(
                StrategyScreeningResult.batch_id == batch_id,
                StrategyScreeningResult.status
                == StrategyScreeningResultStatus.RUNNING.value,
            )
            .values(
                status=StrategyScreeningResultStatus.PENDING.value,
                started_at=None,
            )
        )


class StrategyScreeningApplication:
    def __init__(
        self,
        database: Any,
        *,
        strategies: ScreeningStrategyPort,
        scope: ScreeningScopePort,
        datasets: ScreeningDatasetPort,
        forecasts: ScreeningForecastPort,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._strategies = strategies
        self._scope = scope
        self._datasets = datasets
        self._forecasts = forecasts
        self._clock = clock

    async def create(
        self,
        request: StrategyScreeningCreateRequest,
        *,
        request_id: str,
        actor_user_id: str,
    ) -> StrategyScreeningBatchView:
        version = await self._published_version(request.strategy_version_id)
        market = await self._scope.list_market()
        names = {item.security_id: item.name for item in market}
        async with self._database.transaction() as session:
            repository = ScreeningRepository(session)
            existing = await repository.get_by_idempotency(request.idempotency_key)
            request_hash = _request_hash(request)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise _error(
                        StrategyScreeningErrorCode.IDEMPOTENCY_CONFLICT,
                        "幂等键已用于不同的筛选请求",
                    )
                batch_id = existing.id
            else:
                frozen = await self._scope.freeze_in_transaction(session)
                if not frozen.items:
                    raise _error(
                        StrategyScreeningErrorCode.SCOPE_EMPTY,
                        "当前没有可用于策略筛选的A股",
                    )
                datasets = await self._datasets.current_dataset_snapshots(
                    tuple(item.security_id for item in frozen.items)
                )
                by_security = {
                    item.security_id: item
                    for item in datasets
                    if item.freshness is QfqFreshness.FRESH
                }
                batch = StrategyScreeningBatch(
                    strategy_version_id=version.id,
                    security_universe_snapshot_id=frozen.id,
                    parameter_snapshot=dict(request.parameter_snapshot),
                    parameter_hash=_parameter_hash(request.parameter_snapshot),
                    request_hash=request_hash,
                    idempotency_key=request.idempotency_key,
                    created_by_user_id=actor_user_id,
                    status=StrategyScreeningBatchStatus.PENDING.value,
                )
                session.add(batch)
                await session.flush()
                periods = [
                    StrategyScreeningPeriod(
                        id=uuid5(batch.id, f"period:{period.sequence_no}"),
                        batch_id=batch.id,
                        sequence_no=period.sequence_no,
                        training_start_date=period.training_start_date,
                        training_end_date=period.training_end_date,
                        test_start_date=period.test_start_date,
                        test_end_date=period.test_end_date,
                    )
                    for period in request.periods
                ]
                scope_items = []
                results = []
                for item in frozen.items:
                    dataset = by_security.get(item.security_id)
                    scope_item = StrategyScreeningScopeItem(
                        id=uuid5(batch.id, f"scope:{item.security_id}"),
                        batch_id=batch.id,
                        security_id=item.security_id,
                        symbol=item.symbol,
                        name=names.get(item.security_id, item.symbol),
                        qfq_dataset_id=dataset.id if dataset else None,
                        qfq_data_version=dataset.version if dataset else None,
                        qfq_data_hash=dataset.checksum if dataset else None,
                    )
                    scope_items.append(scope_item)
                    results.extend(
                        StrategyScreeningResult(
                            id=uuid5(
                                batch.id,
                                f"result:{item.security_id}:{period.sequence_no}",
                            ),
                            batch_id=batch.id,
                            period_id=period.id,
                            scope_item_id=scope_item.id,
                            status=StrategyScreeningResultStatus.PENDING.value,
                        )
                        for period in periods
                    )
                session.add_all([*periods, *scope_items, *results])
                job = await PostgresJobService(session).submit(
                    SubmitPostgresJob(
                        job_type="STRATEGY_SCREENING_BATCH",
                        module_owner="strategies",
                        idempotency_scope="strategy-screening",
                        idempotency_key=str(batch.id),
                        request_id=request_id,
                        config_snapshot={
                            "batch_id": str(batch.id),
                            "concurrency": request.concurrency,
                        },
                        priority=2,
                        business_object_type="strategy_screening_batch",
                        business_object_id=str(batch.id),
                        created_by_user_id=actor_user_id,
                        soft_timeout_seconds=82800,
                        hard_timeout_seconds=86400,
                        recoverable=True,
                        max_recoveries=20,
                    )
                )
                batch.job_id = job.id
                batch_id = batch.id
        return await self.get(batch_id)

    async def get(self, batch_id: UUID) -> StrategyScreeningBatchView:
        async with self._database.session() as session:
            repository = ScreeningRepository(session)
            batch = await repository.get_batch(batch_id)
            if batch is None:
                raise _not_found()
            return await _batch_view(repository, batch)

    async def list_batches(
        self, *, page: int, page_size: int
    ) -> StrategyScreeningBatchPage:
        async with self._database.session() as session:
            repository = ScreeningRepository(session)
            rows, total = await repository.list_batches(
                page=page, page_size=page_size
            )
            return StrategyScreeningBatchPage(
                items=tuple(
                    [await _batch_view(repository, row) for row in rows]
                ),
                page=page,
                page_size=page_size,
                total=total,
            )

    async def list_results(
        self,
        batch_id: UUID,
        *,
        page: int,
        page_size: int,
        symbol: str | None = None,
        period_id: UUID | None = None,
        status: StrategyScreeningResultStatus | None = None,
    ) -> StrategyScreeningResultPage:
        async with self._database.session() as session:
            repository = ScreeningRepository(session)
            if await repository.get_batch(batch_id) is None:
                raise _not_found()
            rows, total = await repository.list_results(
                batch_id,
                page=page,
                page_size=page_size,
                symbol=symbol,
                period_id=period_id,
                status=status,
            )
            return StrategyScreeningResultPage(
                items=tuple(_result_view(*row) for row in rows),
                page=page,
                page_size=page_size,
                total=total,
            )

    async def candidate_snapshot(
        self, batch_id: UUID
    ) -> StrategyCandidateBatchSnapshot:
        async with self._database.session() as session:
            repository = ScreeningRepository(session)
            batch = await repository.get_batch(batch_id)
            if batch is None:
                raise _not_found()
            if batch.status != StrategyScreeningBatchStatus.SUCCEEDED.value:
                raise _error(
                    StrategyScreeningErrorCode.STATE_CONFLICT,
                    "只有已成功完成的筛选批次可以用于回测",
                )
            rows = await session.execute(
                select(
                    StrategyScreeningResult,
                    StrategyScreeningScopeItem,
                    StrategyScreeningPeriod,
                )
                .join(
                    StrategyScreeningScopeItem,
                    StrategyScreeningScopeItem.id
                    == StrategyScreeningResult.scope_item_id,
                )
                .join(
                    StrategyScreeningPeriod,
                    StrategyScreeningPeriod.id == StrategyScreeningResult.period_id,
                )
                .where(
                    StrategyScreeningResult.batch_id == batch_id,
                    StrategyScreeningResult.status
                    == StrategyScreeningResultStatus.MATCHED.value,
                )
                .order_by(
                    StrategyScreeningScopeItem.symbol,
                    StrategyScreeningPeriod.sequence_no,
                )
            )
            items = []
            for result, scope, period in rows.tuples():
                if (
                    scope.qfq_dataset_id is None
                    or scope.qfq_data_version is None
                    or scope.qfq_data_hash is None
                    or result.training_data_hash is None
                    or result.training_row_count is None
                ):
                    raise _state_conflict()
                items.append(
                    StrategyCandidateItem(
                        screening_result_id=result.id,
                        screening_period_id=period.id,
                        period_sequence=period.sequence_no,
                        security_id=scope.security_id,
                        symbol=scope.symbol,
                        name=scope.name,
                        training_start_date=period.training_start_date,
                        training_end_date=period.training_end_date,
                        test_start_date=period.test_start_date,
                        test_end_date=period.test_end_date,
                        qfq_dataset_id=scope.qfq_dataset_id,
                        qfq_data_version=scope.qfq_data_version,
                        qfq_data_hash=scope.qfq_data_hash,
                        training_data_hash=result.training_data_hash,
                        training_row_count=result.training_row_count,
                        values=TargetValues(
                            low_strong=result.low_strong,
                            low_watch=result.low_watch,
                            high_watch=result.high_watch,
                            high_strong=result.high_strong,
                        ),
                    )
                )
            try:
                return StrategyCandidateBatchSnapshot(
                    batch_id=batch.id,
                    strategy_version_id=batch.strategy_version_id,
                    parameter_snapshot=batch.parameter_snapshot,
                    parameter_hash=batch.parameter_hash,
                    items=tuple(items),
                )
            except ValueError as exc:
                raise _error(
                    StrategyScreeningErrorCode.STATE_CONFLICT,
                    "筛选批次没有可用于回测的候选股票",
                ) from exc

    async def control(
        self,
        batch_id: UUID,
        action: StrategyScreeningAction,
        *,
        idempotency_key: str,
        actor_user_id: str,
        reason: str,
    ) -> StrategyScreeningBatchView:
        async with self._database.transaction() as session:
            repository = ScreeningRepository(session)
            batch = await repository.get_batch(batch_id, for_update=True)
            if batch is None:
                raise _not_found()
            control_hash = hashlib.sha256(
                _canonical(
                    {
                        "batch_id": str(batch_id),
                        "action": action.value,
                        "reason": reason,
                    }
                )
            ).hexdigest()
            existing = await session.scalar(
                select(StrategyScreeningControlCommand).where(
                    StrategyScreeningControlCommand.idempotency_key
                    == idempotency_key
                )
            )
            if existing is not None:
                if (
                    existing.batch_id != batch_id
                    or existing.action != action.value
                    or existing.request_hash != control_hash
                ):
                    raise _error(
                        StrategyScreeningErrorCode.IDEMPOTENCY_CONFLICT,
                        "幂等键已用于不同的筛选控制请求",
                    )
                return await _batch_view(repository, batch)
            status = StrategyScreeningBatchStatus(batch.status)
            if batch.job_id is None:
                raise _state_conflict()
            jobs = PostgresJobService(session)
            if action is StrategyScreeningAction.PAUSE:
                if status not in {
                    StrategyScreeningBatchStatus.PENDING,
                    StrategyScreeningBatchStatus.RUNNING,
                }:
                    raise _state_conflict()
                await jobs.command(batch.job_id, "pause")
                batch.status = (
                    StrategyScreeningBatchStatus.PAUSED.value
                    if status is StrategyScreeningBatchStatus.PENDING
                    else StrategyScreeningBatchStatus.PAUSING.value
                )
            elif action is StrategyScreeningAction.RESUME:
                if status is not StrategyScreeningBatchStatus.PAUSED:
                    raise _state_conflict()
                await jobs.command(batch.job_id, "resume")
                batch.status = StrategyScreeningBatchStatus.PENDING.value
            elif action is StrategyScreeningAction.CANCEL:
                if status not in {
                    StrategyScreeningBatchStatus.PENDING,
                    StrategyScreeningBatchStatus.RUNNING,
                    StrategyScreeningBatchStatus.PAUSING,
                    StrategyScreeningBatchStatus.PAUSED,
                }:
                    raise _state_conflict()
                await jobs.command(batch.job_id, "cancel")
                if status in {
                    StrategyScreeningBatchStatus.PENDING,
                    StrategyScreeningBatchStatus.PAUSED,
                }:
                    await self._cancel_pending(session, batch)
                else:
                    batch.status = StrategyScreeningBatchStatus.CANCELING.value
            else:
                if status not in {
                    StrategyScreeningBatchStatus.PARTIAL,
                    StrategyScreeningBatchStatus.FAILED,
                }:
                    raise _state_conflict()
                failed_ids = list(
                    await session.scalars(
                        select(StrategyScreeningResult.id).where(
                            StrategyScreeningResult.batch_id == batch.id,
                            StrategyScreeningResult.status
                            == StrategyScreeningResultStatus.FAILED.value,
                        )
                    )
                )
                if not failed_ids:
                    raise _state_conflict()
                await session.execute(
                    update(StrategyScreeningResult)
                    .where(StrategyScreeningResult.id.in_(failed_ids))
                    .values(
                        status=StrategyScreeningResultStatus.PENDING.value,
                        failure_code=None,
                        started_at=None,
                        ended_at=None,
                    )
                )
                await jobs.command(batch.job_id, "retry")
                batch.status = StrategyScreeningBatchStatus.PENDING.value
                batch.completed_at = None
            session.add(
                StrategyScreeningControlCommand(
                    batch_id=batch.id,
                    action=action.value,
                    idempotency_key=idempotency_key,
                    request_hash=control_hash,
                    created_by_user_id=actor_user_id,
                )
            )
            batch.updated_at = self._clock()
        return await self.get(batch_id)

    async def run(self, context: JobExecutionContext) -> JobResult:
        try:
            batch_id = UUID(str(context.config["batch_id"]))
            concurrency = int(context.config.get("concurrency", 4))
            if not 1 <= concurrency <= 64:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return JobResult.failure(
                code="STRATEGY_SCREENING_CONFIG_INVALID",
                message="策略筛选任务配置无效",
                retryable=False,
            )
        batch = await self._prepare_run(batch_id)
        version = await self._published_version(batch.strategy_version_id)
        while True:
            action = await self._run_state(batch_id)
            if action in {"PAUSE", "CANCEL"}:
                return await self._controlled_stop(context, batch_id, action)
            work = await self._next_work(batch_id, concurrency)
            if not work:
                break
            await asyncio.gather(
                *(self._run_one(version, batch, *row) for row in work)
            )
            await self._report_progress(context, batch_id)
        view = await self._finish_batch(batch_id)
        failed_ids = await self._failed_result_ids(batch_id)
        data = view.model_dump(mode="json")
        data["failed_items"] = [str(value) for value in failed_ids]
        if view.status is StrategyScreeningBatchStatus.FAILED:
            return JobResult.failure(
                code="STRATEGY_SCREENING_FAILED",
                message="全市场策略筛选全部失败",
                retryable=False,
                data=data,
            )
        return JobResult(
            success=True,
            code=(
                "SUCCESS"
                if view.status is StrategyScreeningBatchStatus.SUCCEEDED
                else "PARTIAL"
            ),
            message=(
                "全市场策略筛选完成"
                if view.status is StrategyScreeningBatchStatus.SUCCEEDED
                else "全市场策略筛选部分完成"
            ),
            retryable=False,
            data=data,
        )

    async def _published_version(self, version_id: UUID):
        version = await self._strategies.get_execution_snapshot(version_id)
        if version is None or str(version.status) != "PUBLISHED":
            raise _error(
                StrategyScreeningErrorCode.VERSION_NOT_PUBLISHED,
                "只能使用已发布且可用的策略版本",
                422,
            )
        return version

    async def _prepare_run(self, batch_id: UUID) -> StrategyScreeningBatch:
        async with self._database.transaction() as session:
            repository = ScreeningRepository(session)
            batch = await repository.get_batch(batch_id, for_update=True)
            if batch is None:
                raise _not_found()
            status = StrategyScreeningBatchStatus(batch.status)
            if status in _TERMINAL_BATCH_STATUSES:
                return batch
            if status not in {
                StrategyScreeningBatchStatus.PENDING,
                StrategyScreeningBatchStatus.RUNNING,
                StrategyScreeningBatchStatus.PARTIAL,
                StrategyScreeningBatchStatus.FAILED,
            }:
                raise _state_conflict()
            await repository.recover_inflight(batch_id)
            batch.status = StrategyScreeningBatchStatus.RUNNING.value
            batch.completed_at = None
            batch.updated_at = self._clock()
            return batch

    async def _run_state(self, batch_id: UUID) -> str | None:
        async with self._database.session() as session:
            batch = await ScreeningRepository(session).get_batch(batch_id)
            if batch is None:
                raise _not_found()
            if batch.status == StrategyScreeningBatchStatus.PAUSING.value:
                return "PAUSE"
            if batch.status == StrategyScreeningBatchStatus.CANCELING.value:
                return "CANCEL"
            return None

    async def _next_work(
        self, batch_id: UUID, concurrency: int
    ) -> list[tuple[Any, ...]]:
        async with self._database.session() as session:
            return await ScreeningRepository(session).work_items(
                batch_id, limit=concurrency
            )

    async def _run_one(self, version, batch, result, scope, period) -> None:
        now = self._clock()
        async with self._database.transaction() as session:
            claimed = await ScreeningRepository(session).claim_result(result.id, now)
        if not claimed:
            return
        if scope.qfq_dataset_id is None:
            await self._fail_result(
                result.id,
                StrategyScreeningErrorCode.TRAINING_DATA_UNAVAILABLE.value,
            )
            return
        try:
            training = await self._datasets.get_training_data_from_dataset(
                dataset_id=scope.qfq_dataset_id,
                security_id=scope.security_id,
                start_date=period.training_start_date,
                end_date=period.training_end_date,
            )
            if training is None:
                raise _TrainingUnavailable
            forecast = await self._forecasts.forecast(
                StrategyForecastRequest(
                    strategy_id=version.strategy_id,
                    security_name=scope.name,
                    strategy_version_id=version.id,
                    source_code=version.source_code,
                    source_code_hash=version.source_code_hash,
                    metadata=version.metadata,
                    parameter_schema=version.parameter_schema,
                    environment_version=version.environment_version,
                    runner_image_digest=version.runner_image_digest,
                    parameter_snapshot=batch.parameter_snapshot,
                    parameter_hash=batch.parameter_hash,
                    training_data=training,
                    requested_at=now,
                )
            )
        except _TrainingUnavailable:
            await self._fail_result(
                result.id,
                StrategyScreeningErrorCode.TRAINING_DATA_UNAVAILABLE.value,
            )
            return
        except (StrategyRunnerFailure, StrategyForecastFailure) as exc:
            code = str(getattr(exc, "code", ""))
            mapped = (
                StrategyScreeningErrorCode.STRATEGY_TIMEOUT.value
                if "TIMEOUT" in code
                else StrategyScreeningErrorCode.STRATEGY_FAILED.value
            )
            await self._fail_result(result.id, mapped)
            return
        except Exception:
            await self._fail_result(
                result.id, StrategyScreeningErrorCode.STRATEGY_FAILED.value
            )
            return
        values = forecast.values
        async with self._database.transaction() as session:
            changes: dict[str, Any] = {
                "status": (
                    StrategyScreeningResultStatus.MATCHED.value
                    if forecast.matched
                    else StrategyScreeningResultStatus.NOT_MATCHED.value
                ),
                "reason": forecast.reason,
                "diagnostics": dict(forecast.diagnostics),
                "training_data_hash": training.content_hash,
                "training_row_count": len(training.rows),
                "failure_code": None,
                "ended_at": self._clock(),
            }
            if values is not None:
                changes.update(
                    low_strong=values.low_strong,
                    low_watch=values.low_watch,
                    high_watch=values.high_watch,
                    high_strong=values.high_strong,
                )
            await session.execute(
                update(StrategyScreeningResult)
                .where(
                    StrategyScreeningResult.id == result.id,
                    StrategyScreeningResult.status
                    == StrategyScreeningResultStatus.RUNNING.value,
                )
                .values(**changes)
            )

    async def _fail_result(self, result_id: UUID, code: str) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                update(StrategyScreeningResult)
                .where(
                    StrategyScreeningResult.id == result_id,
                    StrategyScreeningResult.status
                    == StrategyScreeningResultStatus.RUNNING.value,
                )
                .values(
                    status=StrategyScreeningResultStatus.FAILED.value,
                    failure_code=code,
                    ended_at=self._clock(),
                )
            )

    async def _controlled_stop(
        self, context: JobExecutionContext, batch_id: UUID, action: str
    ) -> JobResult:
        async with self._database.transaction() as session:
            repository = ScreeningRepository(session)
            batch = await repository.get_batch(batch_id, for_update=True)
            if batch is None:
                raise _not_found()
            jobs = PostgresJobService(session)
            counts = await repository.counts(batch_id)
            completed = sum(counts.get(item, 0) for item in _FINAL_RESULT_STATUSES)
            total = sum(counts.values())
            if action == "PAUSE":
                batch.status = StrategyScreeningBatchStatus.PAUSED.value
                await jobs.report_progress(
                    context.job_id,
                    context.fence_token,
                    progress=JobProgress(completed=completed, total=total),
                    checkpoint={},
                    lease_duration=timedelta(seconds=60),
                    pause=True,
                )
            else:
                await self._cancel_pending(session, batch)
                await jobs.command(context.job_id, "cancel")
        return JobResult.success_result(
            message="策略筛选已暂停" if action == "PAUSE" else "策略筛选已停止",
            data={"batch_id": str(batch_id), "status": batch.status},
        )

    async def _cancel_pending(self, session: Any, batch) -> None:
        now = self._clock()
        await session.execute(
            update(StrategyScreeningResult)
            .where(
                StrategyScreeningResult.batch_id == batch.id,
                StrategyScreeningResult.status.in_(
                    (
                        StrategyScreeningResultStatus.PENDING.value,
                        StrategyScreeningResultStatus.RUNNING.value,
                    )
                ),
            )
            .values(
                status=StrategyScreeningResultStatus.CANCELED.value,
                ended_at=now,
            )
        )
        batch.status = StrategyScreeningBatchStatus.CANCELED.value
        batch.completed_at = now
        batch.updated_at = now

    async def _report_progress(
        self, context: JobExecutionContext, batch_id: UUID
    ) -> None:
        async with self._database.transaction() as session:
            counts = await ScreeningRepository(session).counts(batch_id)
            total = sum(counts.values())
            completed = sum(counts.get(item, 0) for item in _FINAL_RESULT_STATUSES)
            await PostgresJobService(session).report_progress(
                context.job_id,
                context.fence_token,
                progress=JobProgress(completed=completed, total=total),
                checkpoint={},
                lease_duration=timedelta(seconds=60),
            )

    async def _finish_batch(
        self, batch_id: UUID
    ) -> StrategyScreeningBatchView:
        async with self._database.transaction() as session:
            repository = ScreeningRepository(session)
            batch = await repository.get_batch(batch_id, for_update=True)
            if batch is None:
                raise _not_found()
            counts = await repository.counts(batch_id)
            failures = counts.get(StrategyScreeningResultStatus.FAILED.value, 0)
            successes = counts.get(StrategyScreeningResultStatus.MATCHED.value, 0)
            successes += counts.get(
                StrategyScreeningResultStatus.NOT_MATCHED.value, 0
            )
            batch.status = (
                StrategyScreeningBatchStatus.SUCCEEDED.value
                if failures == 0
                else (
                    StrategyScreeningBatchStatus.PARTIAL.value
                    if successes > 0
                    else StrategyScreeningBatchStatus.FAILED.value
                )
            )
            batch.completed_at = self._clock()
            batch.updated_at = batch.completed_at
            return await _batch_view(repository, batch)

    async def _failed_result_ids(self, batch_id: UUID) -> list[UUID]:
        async with self._database.session() as session:
            rows = await session.scalars(
                select(StrategyScreeningResult.id)
                .where(
                    StrategyScreeningResult.batch_id == batch_id,
                    StrategyScreeningResult.status
                    == StrategyScreeningResultStatus.FAILED.value,
                )
                .order_by(StrategyScreeningResult.id)
            )
            return list(rows)


class _TrainingUnavailable(Exception):
    pass


async def _batch_view(
    repository: ScreeningRepository, batch: StrategyScreeningBatch
) -> StrategyScreeningBatchView:
    periods = await repository.periods(batch.id)
    counts = await repository.counts(batch.id)
    total = sum(counts.values())
    terminal = sum(counts.get(item, 0) for item in _FINAL_RESULT_STATUSES)
    return StrategyScreeningBatchView(
        id=batch.id,
        strategy_version_id=batch.strategy_version_id,
        security_universe_snapshot_id=batch.security_universe_snapshot_id,
        parameter_snapshot=batch.parameter_snapshot,
        parameter_hash=batch.parameter_hash,
        status=StrategyScreeningBatchStatus(batch.status),
        periods=tuple(
            ScreeningPeriodContract(
                sequence_no=period.sequence_no,
                training_start_date=period.training_start_date,
                training_end_date=period.training_end_date,
                test_start_date=period.test_start_date,
                test_end_date=period.test_end_date,
            )
            for period in periods
        ),
        total_items=total,
        matched_items=counts.get(StrategyScreeningResultStatus.MATCHED.value, 0),
        not_matched_items=counts.get(
            StrategyScreeningResultStatus.NOT_MATCHED.value, 0
        ),
        failed_items=counts.get(StrategyScreeningResultStatus.FAILED.value, 0),
        canceled_items=counts.get(
            StrategyScreeningResultStatus.CANCELED.value, 0
        ),
        pending_items=total - terminal,
        allowed_actions=_allowed_actions(StrategyScreeningBatchStatus(batch.status)),
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        completed_at=batch.completed_at,
    )


def _result_view(result, scope, period) -> StrategyScreeningResultView:
    values = None
    if result.status == StrategyScreeningResultStatus.MATCHED.value:
        values = TargetValues(
            low_strong=result.low_strong,
            low_watch=result.low_watch,
            high_watch=result.high_watch,
            high_strong=result.high_strong,
        )
    return StrategyScreeningResultView(
        id=result.id,
        batch_id=result.batch_id,
        period_id=result.period_id,
        period_sequence=period.sequence_no,
        scope_item_id=result.scope_item_id,
        security_id=scope.security_id,
        symbol=scope.symbol,
        name=scope.name,
        status=StrategyScreeningResultStatus(result.status),
        values=values,
        reason=result.reason,
        failure_code=result.failure_code,
        diagnostics=result.diagnostics,
        training_data_hash=result.training_data_hash,
        training_row_count=result.training_row_count,
        attempt_count=result.attempt_count,
        started_at=result.started_at,
        ended_at=result.ended_at,
    )


def _allowed_actions(
    status: StrategyScreeningBatchStatus,
) -> tuple[StrategyScreeningAction, ...]:
    if status in {
        StrategyScreeningBatchStatus.PENDING,
        StrategyScreeningBatchStatus.RUNNING,
    }:
        return (StrategyScreeningAction.PAUSE, StrategyScreeningAction.CANCEL)
    if status is StrategyScreeningBatchStatus.PAUSED:
        return (StrategyScreeningAction.RESUME, StrategyScreeningAction.CANCEL)
    if status in {
        StrategyScreeningBatchStatus.PARTIAL,
        StrategyScreeningBatchStatus.FAILED,
    }:
        return (StrategyScreeningAction.RETRY_FAILED,)
    return ()


def _request_hash(request: StrategyScreeningCreateRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _parameter_hash(parameters: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(parameters))).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()


def _not_found() -> AppError:
    return _error(
        StrategyScreeningErrorCode.NOT_FOUND,
        "策略筛选批次不存在",
        404,
    )


def _state_conflict() -> AppError:
    return _error(
        StrategyScreeningErrorCode.STATE_CONFLICT,
        "策略筛选批次当前状态不允许执行该操作",
    )


def _error(
    code: StrategyScreeningErrorCode,
    message: str,
    status_code: int = 409,
) -> AppError:
    return AppError(code=code.value, message=message, status_code=status_code)
