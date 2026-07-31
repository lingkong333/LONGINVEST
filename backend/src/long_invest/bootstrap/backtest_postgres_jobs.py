from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid5

from long_invest.bootstrap.stage4_runtime import build_backtest_application
from long_invest.modules.backtests.application import build_backtest_job_handler
from long_invest.modules.backtests.contracts import BacktestMode
from long_invest.platform.database.engine import get_database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobProgress,
    JobResult,
    JobStatus,
)
from long_invest.platform.jobs.postgres_service import PostgresJobService

_TERMINAL_ITEM_STATUSES = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELED"}


async def backtest_single(context: JobExecutionContext) -> JobResult:
    result = await build_backtest_job_handler(build_backtest_application())(context)
    await _sync_controlled_stop(context, result)
    return result


async def backtest_batch(context: JobExecutionContext) -> JobResult:
    try:
        task_id = UUID(str(context.config["backtest_task_id"]))
        generation = int(context.config["generation"])
        concurrency = int(context.config["concurrency"])
        item_keys = tuple(str(value) for value in context.config["item_keys"])
        next_index = int(context.checkpoint.get("next_index", 0))
        if (
            generation < 1
            or concurrency < 1
            or not item_keys
            or len(item_keys) != len(set(item_keys))
            or not 0 <= next_index <= len(item_keys)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="BACKTEST_BATCH_CONFIG_INVALID",
            message="批量回测任务配置无效",
            retryable=False,
        )

    application = build_backtest_application()
    state = await application.get_execution(task_id)
    if state.task.mode not in {BacktestMode.WATCHLIST, BacktestMode.MARKET}:
        return JobResult.failure(
            code="BACKTEST_BATCH_SCOPE_INVALID",
            message="批量回测只能处理监控列表或全市场范围",
            retryable=False,
        )
    entries = {entry.symbol: entry for entry in state.task.universe_snapshot}
    if any(key not in entries for key in item_keys):
        return JobResult.failure(
            code="BACKTEST_BATCH_SCOPE_INVALID",
            message="批量回测范围与冻结快照不一致",
            retryable=False,
        )

    recover = bool(context.config.get("recover")) or next_index > 0
    for start in range(next_index, len(item_keys), concurrency):
        keys = item_keys[start : start + concurrency]
        await asyncio.gather(
            *(
                _run_item(
                    application,
                    task_id=task_id,
                    item_id=uuid5(task_id, f"item:{entries[key].security_id}"),
                    generation=generation,
                    execution_token=context.fence_token,
                    recover=recover,
                )
                for key in keys
            )
        )
        next_index = start + len(keys)
        summary = await application.get_summary(task_id)
        status = await _report_progress(
            context,
            completed=next_index,
            total=len(item_keys),
            checkpoint={"next_index": next_index},
            task_status=summary.status.value,
        )
        if status in {JobStatus.PAUSED, JobStatus.CANCELED}:
            return JobResult.success_result(
                message="批量回测已按任务控制安全停止",
                data=summary.model_dump(mode="json"),
            )
        recover = False

    summary = await application.get_summary(task_id)
    data = summary.model_dump(mode="json")
    if summary.succeeded_items == 0:
        return JobResult.failure(
            code="BACKTEST_BATCH_FAILED",
            message="批量回测全部失败",
            retryable=False,
            data=data,
        )
    return JobResult(
        success=True,
        code="SUCCESS" if summary.failed_items == 0 else "PARTIAL",
        message=(
            "批量回测完成" if summary.failed_items == 0 else "批量回测部分完成"
        ),
        retryable=False,
        data=data,
    )


async def _run_item(
    application,
    *,
    task_id: UUID,
    item_id: UUID,
    generation: int,
    execution_token: UUID,
    recover: bool,
) -> None:
    try:
        if recover:
            await application.recover(
                task_id,
                item_id=item_id,
                generation=generation,
                execution_token=execution_token,
            )
        else:
            await application.run_item(
                task_id,
                item_id=item_id,
                generation=generation,
                execution_token=execution_token,
            )
    except AppError as exc:
        if exc.code not in {
            "BACKTEST_PAUSED",
            "BACKTEST_PAUSING",
            "BACKTEST_CANCELED",
            "BACKTEST_CANCELING",
        }:
            raise


async def _report_progress(
    context: JobExecutionContext,
    *,
    completed: int,
    total: int,
    checkpoint: dict[str, object],
    task_status: str,
) -> JobStatus | None:
    database = get_database()
    async with database.transaction() as session:
        jobs = PostgresJobService(session)
        if task_status in {"CANCELED", "CANCELING"}:
            await jobs.command(context.job_id, "cancel")
        return await jobs.report_progress(
            context.job_id,
            context.fence_token,
            progress=JobProgress(completed=completed, total=total),
            checkpoint=checkpoint,
            lease_duration=timedelta(seconds=60),
            pause=task_status in {"PAUSED", "PAUSING"},
        )


async def _sync_controlled_stop(
    context: JobExecutionContext, result: JobResult
) -> None:
    task_status = str(result.data.get("task_status", result.data.get("status", "")))
    if task_status not in {"PAUSED", "PAUSING", "CANCELED", "CANCELING"}:
        return
    item_status = str(result.data.get("status", ""))
    await _report_progress(
        context,
        completed=int(item_status in _TERMINAL_ITEM_STATUSES),
        total=1,
        checkpoint={},
        task_status=task_status,
    )
