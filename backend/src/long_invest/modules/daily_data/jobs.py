from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from long_invest.modules.daily_data.contracts import (
    CreateDailyBatch,
    DailyBatchStatus,
    DailyMissingReason,
    DailyStageStatus,
    StageDailyBar,
)
from long_invest.modules.daily_data.outbox import DailyDataEventWriter
from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.modules.daily_data.service import DailyDataService
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import QualityIssueService
from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyBarRequest,
    DailyCollectionMode,
    DailyCollectionPlan,
    MarketDailyGroupRequest,
    ProviderCapability,
    ProviderCode,
)
from long_invest.modules.securities.application import SecurityApplication
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobProgress,
    JobResult,
    JobStatus,
)
from long_invest.platform.jobs.postgres_service import PostgresJobService


class FullMarketDailyJob:
    def __init__(
        self,
        database: Any,
        *,
        provider_service_factory: Callable[[Any], Any],
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._providers = provider_service_factory
        self._now = now_provider or (lambda: datetime.now(UTC))

    async def __call__(self, context: JobExecutionContext) -> JobResult:
        try:
            trading_date = date.fromisoformat(
                str(
                    context.config.get("trade_date")
                    or context.config["trading_date"]
                )
            )
        except (KeyError, TypeError, ValueError):
            return JobResult.failure(
                code="DAILY_MARKET_CONFIG_INVALID",
                message="全市场日线任务缺少有效交易日期",
                retryable=False,
            )

        checkpoint = dict(context.checkpoint)
        if checkpoint:
            try:
                snapshot_id = UUID(str(checkpoint["universe_snapshot_id"]))
                batch_id = UUID(str(checkpoint["batch_id"]))
                plan = _restore_plan(checkpoint["plan"])
                next_group = int(checkpoint.get("next_group", 0))
            except (KeyError, TypeError, ValueError):
                return JobResult.failure(
                    code="DAILY_MARKET_CHECKPOINT_INVALID",
                    message="全市场日线检查点无效",
                    retryable=False,
                )
            frozen = await SecurityApplication(self._database).frozen_universe(
                snapshot_id
            )
            async with self._database.session() as session:
                stored_batch = await DailyDataRepository(session).get_batch(batch_id)
            if stored_batch is None:
                return JobResult.failure(
                    code="DAILY_BATCH_NOT_FOUND",
                    message="全市场日线检查点对应批次不存在",
                    retryable=False,
                )
            if DailyBatchStatus(stored_batch.status) in {
                DailyBatchStatus.SUCCEEDED,
                DailyBatchStatus.PARTIAL,
                DailyBatchStatus.FAILED,
            }:
                return _result(stored_batch)
            batch_symbols = set(stored_batch.symbols)
            frozen_items = tuple(
                item for item in frozen.items if item.symbol in batch_symbols
            )
        else:
            try:
                parent_batch_id = (
                    UUID(str(context.config["original_batch_id"]))
                    if context.config.get("original_batch_id")
                    else None
                )
            except (TypeError, ValueError):
                return JobResult.failure(
                    code="DAILY_MARKET_CONFIG_INVALID",
                    message="日线重试任务的原批次编号无效",
                    retryable=False,
                )
            idempotency_key = (
                f"daily-retry:{context.job_id}"
                if parent_batch_id is not None
                else f"daily-market:{trading_date.isoformat()}"
            )
            async with self._database.session() as session:
                existing = await DailyDataRepository(
                    session
                ).get_batch_by_idempotency_key(idempotency_key)
            if existing is not None:
                frozen = await SecurityApplication(self._database).frozen_universe(
                    existing.universe_snapshot_id
                )
                existing_symbols = set(existing.symbols)
                frozen_items = tuple(
                    item for item in frozen.items if item.symbol in existing_symbols
                )
                plan = _restore_plan(existing.plan_snapshot)
                batch = existing
                if DailyBatchStatus(existing.status) in {
                    DailyBatchStatus.SUCCEEDED,
                    DailyBatchStatus.PARTIAL,
                    DailyBatchStatus.FAILED,
                }:
                    return _result(existing)
            else:
                if parent_batch_id is None:
                    async with self._database.transaction() as session:
                        frozen = await SecurityApplication(
                            self._database
                        ).freeze_daily_universe_in_transaction(session)
                    frozen_items = frozen.items
                else:
                    try:
                        retry_snapshot_id = UUID(
                            str(context.config["universe_snapshot_id"])
                        )
                        requested = tuple(
                            str(item) for item in context.config["symbols"]
                        )
                    except (KeyError, TypeError, ValueError):
                        return JobResult.failure(
                            code="DAILY_MARKET_CONFIG_INVALID",
                            message="日线重试任务的冻结范围无效",
                            retryable=False,
                        )
                    frozen = await SecurityApplication(
                        self._database
                    ).frozen_universe(retry_snapshot_id)
                    requested_set = set(requested)
                    frozen_items = tuple(
                        item for item in frozen.items if item.symbol in requested_set
                    )
                    if len(requested_set) != len(requested) or len(frozen_items) != len(
                        requested
                    ):
                        return JobResult.failure(
                            code="DAILY_MARKET_SCOPE_INVALID",
                            message="日线重试范围与冻结股票范围不一致",
                            retryable=False,
                        )
                if not frozen_items:
                    return JobResult.failure(
                        code="DAILY_MARKET_UNIVERSE_EMPTY",
                        message="全市场股票范围为空，未启动日线采集",
                        retryable=False,
                    )
                async with self._database.session() as session:
                    providers = self._providers(session)
                    plan = await providers.daily_collection_plan(
                        len(frozen_items)
                    )
                    budget = await providers.budget(plan.provider)
                command = CreateDailyBatch(
                    trading_date=trading_date,
                    universe_snapshot_id=frozen.id,
                    symbols=tuple(item.symbol for item in frozen_items),
                    security_ids=tuple(item.security_id for item in frozen_items),
                    idempotency_key=idempotency_key,
                    deadline_at=self._now() + timedelta(hours=2),
                    plan_snapshot=_plan_snapshot(plan, budget=budget),
                    parent_batch_id=parent_batch_id,
                    known_corporate_action_symbols=tuple(
                        str(item)
                        for item in context.config.get(
                            "known_corporate_action_symbols", ()
                        )
                    ),
                )
                async with self._database.transaction() as session:
                    batch = await DailyDataService(
                        DailyDataRepository(session), now_provider=self._now
                    ).create(command)
            snapshot_id = frozen.id
            batch_id = batch.id
            next_group = 0
            checkpoint = _checkpoint(snapshot_id, batch_id, plan, next_group)
            status = await self._report(
                context,
                completed=0,
                total=plan.estimated_requests,
                message="已冻结全市场股票范围和采集计划",
                checkpoint=checkpoint,
            )
            if status in {JobStatus.CANCELED, JobStatus.PAUSED}:
                return _stopped(status, batch_id)

        symbols = tuple(item.symbol for item in frozen_items)
        security_by_symbol = {item.symbol: item for item in frozen_items}
        groups = _groups(plan, symbols)
        if next_group > len(groups):
            return JobResult.failure(
                code="DAILY_MARKET_CHECKPOINT_INVALID",
                message="全市场日线检查点超过采集范围",
                retryable=False,
            )

        for index in range(next_group, len(groups)):
            group_symbols = groups[index]
            try:
                result = await self._fetch_group(
                    plan,
                    MarketDailyGroupRequest(trading_date, group_symbols, index),
                )
                stages = _group_stages(
                    result,
                    expected_symbols=(
                        ()
                        if plan.mode is DailyCollectionMode.PAGED
                        else group_symbols
                    ),
                    security_by_symbol=security_by_symbol,
                    trading_date=trading_date,
                    now=self._now(),
                )
            except Exception as error:
                stages = tuple(
                    _failed_stage(
                        security_by_symbol[symbol],
                        trading_date,
                        getattr(error, "code", "DAILY_PROVIDER_FAILED"),
                        self._now(),
                    )
                    for symbol in (
                        () if plan.mode is DailyCollectionMode.PAGED else group_symbols
                    )
                )
            if stages:
                async with self._database.transaction() as session:
                    await DailyDataService(
                        DailyDataRepository(session), now_provider=self._now
                    ).stage_many(
                        batch_id,
                        stages,
                        requested_count=min(
                            (index + 1) * plan.group_size, plan.total_symbols
                        ),
                    )
            checkpoint = _checkpoint(snapshot_id, batch_id, plan, index + 1)
            status = await self._report(
                context,
                completed=index + 1,
                total=len(groups),
                message=f"已完成采集分组 {index + 1}/{len(groups)}",
                checkpoint=checkpoint,
            )
            if status in {JobStatus.CANCELED, JobStatus.PAUSED}:
                return _stopped(status, batch_id)
            if index + 1 < len(groups):
                await asyncio.sleep(plan.estimated_seconds_per_request)

        async with self._database.transaction() as session:
            service = DailyDataService(
                DailyDataRepository(session),
                events=DailyDataEventWriter(session),
                quality_issues=QualityIssueService(QualityIssueRepository(session)),
                now_provider=self._now,
            )
            await service.validate(batch_id)
        await self._retry_failed(batch_id, trading_date, security_by_symbol, plan)
        async with self._database.transaction() as session:
            service = DailyDataService(
                DailyDataRepository(session),
                events=DailyDataEventWriter(session),
                quality_issues=QualityIssueService(QualityIssueRepository(session)),
                now_provider=self._now,
            )
            await service.validate(batch_id)
            batch = await service.commit(batch_id)
        return _result(batch)

    async def _fetch_group(
        self, plan: DailyCollectionPlan, request: MarketDailyGroupRequest
    ):
        async with self._database.session() as session:
            return await self._providers(session).market_daily_bars(
                plan,
                request,
                self._now() + timedelta(seconds=180),
            )

    async def _retry_failed(
        self,
        batch_id: UUID,
        trading_date: date,
        security_by_symbol: dict[str, Any],
        plan: DailyCollectionPlan,
    ) -> None:
        async with self._database.session() as session:
            stages = await DailyDataRepository(session).list_stages(batch_id)
        staged = {item.symbol: item for item in stages}
        retry_symbols = tuple(
            symbol
            for symbol in security_by_symbol
            if symbol not in staged
            or DailyStageStatus(staged[symbol].status)
            in {DailyStageStatus.FAILED, DailyStageStatus.INVALID}
        )
        if not retry_symbols:
            return
        retry_stages = []
        for index, symbol in enumerate(retry_symbols):
            try:
                async with self._database.session() as session:
                    result = await self._providers(session).daily_bars(
                        DailyBarRequest(
                            symbol,
                            trading_date,
                            trading_date,
                            ProviderCapability.DAILY_BAR_UNADJUSTED,
                        ),
                        self._now() + timedelta(seconds=180),
                    )
                bar = next(
                    (
                        item
                        for item in result.items
                        if item.symbol == symbol
                        and item.trading_date == trading_date
                    ),
                    None,
                )
                retry_stages.append(
                    _bar_stage(bar, security_by_symbol[symbol], self._now())
                    if bar is not None
                    else _failed_stage(
                        security_by_symbol[symbol],
                        trading_date,
                        result.batch_error_code or "DAILY_BAR_MISSING",
                        self._now(),
                    )
                )
            except Exception as error:
                retry_stages.append(
                    _failed_stage(
                        security_by_symbol[symbol],
                        trading_date,
                        getattr(error, "code", "DAILY_PROVIDER_FAILED"),
                        self._now(),
                    )
                )
            if index + 1 < len(retry_symbols):
                await asyncio.sleep(plan.estimated_seconds_per_request)
        async with self._database.transaction() as session:
            await DailyDataService(
                DailyDataRepository(session), now_provider=self._now
            ).stage_many(
                batch_id,
                tuple(retry_stages),
                requested_count=plan.total_symbols,
            )

    async def _report(
        self,
        context: JobExecutionContext,
        *,
        completed: int,
        total: int,
        message: str,
        checkpoint: dict[str, object],
    ) -> JobStatus | None:
        async with self._database.transaction() as session:
            return await PostgresJobService(session).report_progress(
                context.job_id,
                context.fence_token,
                progress=JobProgress(completed, total, message),
                checkpoint=checkpoint,
                lease_duration=timedelta(seconds=60),
                now=self._now(),
            )


def _groups(
    plan: DailyCollectionPlan, symbols: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    if plan.mode is DailyCollectionMode.SNAPSHOT:
        return (symbols,)
    if plan.mode is DailyCollectionMode.PAGED:
        return tuple(symbols for _ in range(plan.estimated_requests))
    return tuple(
        symbols[index : index + plan.group_size]
        for index in range(0, len(symbols), plan.group_size)
    )


def _group_stages(
    result: Any,
    *,
    expected_symbols: tuple[str, ...],
    security_by_symbol: dict[str, Any],
    trading_date: date,
    now: datetime,
) -> tuple[StageDailyBar, ...]:
    items = {item.symbol: item for item in result.items}
    failures = {item.symbol: item.code for item in result.failures}
    if len(items) != len(result.items) or any(
        symbol not in security_by_symbol for symbol in items
    ):
        raise ValueError("provider returned an invalid daily market group")
    stages = [
        _bar_stage(item, security_by_symbol[item.symbol], now)
        for item in items.values()
    ]
    for symbol in expected_symbols:
        if symbol not in items:
            stages.append(
                _failed_stage(
                    security_by_symbol[symbol],
                    trading_date,
                    failures.get(symbol)
                    or result.batch_error_code
                    or "DAILY_BAR_MISSING",
                    now,
                )
            )
    return tuple(stages)


def _bar_stage(bar: DailyBar, security: Any, now: datetime) -> StageDailyBar:
    return StageDailyBar(
        symbol=bar.symbol,
        security_id=security.security_id,
        trading_date=bar.trading_date,
        status=DailyStageStatus.FETCHED,
        received_at=now,
        provider_payload={
            "symbol": bar.symbol,
            "trading_date": bar.trading_date,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
            "amount": str(bar.amount),
            "source": bar.source.value,
            "source_identity": (
                {
                    "adapter": bar.source_identity.adapter.value,
                    "upstream": bar.source_identity.upstream.value,
                    "interface": bar.source_identity.interface,
                    "capability": bar.source_identity.capability.value,
                    "algorithm_version": bar.source_identity.algorithm_version,
                }
                if bar.source_identity is not None
                else None
            ),
            "collected_at": (
                bar.collected_at.isoformat() if bar.collected_at else now.isoformat()
            ),
            "is_new_listing": security.listed_on == bar.trading_date,
            "is_st": security.is_st,
            "has_known_corporate_action": False,
        },
    )


def _failed_stage(
    security: Any, trading_date: date, error_code: str, now: datetime
) -> StageDailyBar:
    reason = _known_absence(security, trading_date)
    return StageDailyBar(
        symbol=security.symbol,
        security_id=security.security_id,
        trading_date=trading_date,
        status=(DailyStageStatus.MISSING if reason else DailyStageStatus.FAILED),
        received_at=now,
        missing_reason=reason,
        error_code=(f"DAILY_{reason.value}" if reason else error_code),
    )


def _known_absence(security: Any, trading_date: date) -> DailyMissingReason | None:
    if security.listed_on and trading_date < security.listed_on:
        return DailyMissingReason.NOT_YET_LISTED
    if security.delisted_on and trading_date > security.delisted_on:
        return DailyMissingReason.DELISTED
    if security.is_suspended:
        return DailyMissingReason.SUSPENDED
    return None


def _plan_snapshot(
    plan: DailyCollectionPlan, *, budget: dict[str, Any] | None = None
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "provider": plan.provider.value,
        "mode": plan.mode.value,
        "total_symbols": plan.total_symbols,
        "group_size": plan.group_size,
        "estimated_requests": plan.estimated_requests,
        "estimated_seconds": plan.estimated_seconds,
        "estimated_seconds_per_request": plan.estimated_seconds_per_request,
    }
    if budget is not None:
        snapshot["budget"] = budget
    return snapshot


def _restore_plan(value: Any) -> DailyCollectionPlan:
    item = dict(value)
    return DailyCollectionPlan(
        ProviderCode(item["provider"]),
        DailyCollectionMode(item["mode"]),
        int(item["total_symbols"]),
        int(item["group_size"]),
        float(item["estimated_seconds_per_request"]),
    )


def _checkpoint(
    snapshot_id: UUID,
    batch_id: UUID,
    plan: DailyCollectionPlan,
    next_group: int,
) -> dict[str, object]:
    return {
        "universe_snapshot_id": str(snapshot_id),
        "batch_id": str(batch_id),
        "plan": _plan_snapshot(plan),
        "next_group": next_group,
    }


def _result(batch: Any) -> JobResult:
    status = DailyBatchStatus(batch.status)
    data = {
        "batch_id": str(batch.id),
        "status": status.value,
        "expected_count": batch.expected_count,
        "committed_count": batch.committed_count,
        "missing_count": batch.missing_count,
        "failed_count": batch.failed_count,
    }
    if status is DailyBatchStatus.SUCCEEDED:
        return JobResult.success_result(data=data, message="全市场日线采集完成")
    if status is DailyBatchStatus.PARTIAL:
        return JobResult(True, "PARTIAL", "全市场日线部分完成", False, data)
    return JobResult.failure(
        code="DAILY_BATCH_FAILED",
        message="全市场日线没有可提交数据",
        retryable=False,
        data=data,
    )


def _stopped(status: JobStatus, batch_id: UUID) -> JobResult:
    return JobResult(
        success=True,
        code=f"JOB_{status.value}",
        message="全市场日线任务已在安全检查点停止",
        retryable=False,
        data={"batch_id": str(batch_id)},
    )
