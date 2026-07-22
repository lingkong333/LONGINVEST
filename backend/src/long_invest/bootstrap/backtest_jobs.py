from __future__ import annotations

from uuid import UUID, uuid5

from long_invest.bootstrap.stage4_runtime import build_backtest_application
from long_invest.modules.backtests.contracts import BacktestMode
from long_invest.platform.database.engine import get_database
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobItemStatus,
    JobResult,
    SubmitJob,
    linked_job_item,
)
from long_invest.platform.jobs.service import JobService


async def backtest_bulk_coordinate(context: JobExecutionContext) -> JobResult:
    try:
        task_id = UUID(str(context.config["backtest_task_id"]))
        generation = int(context.config["generation"])
        if generation < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="BACKTEST_BULK_CONFIG_INVALID",
            message="批量回测任务配置无效",
            retryable=False,
        )

    state = await build_backtest_application().get_execution(task_id)
    if state.task.mode not in {BacktestMode.WATCHLIST, BacktestMode.MARKET}:
        return JobResult.failure(
            code="BACKTEST_BULK_SCOPE_INVALID",
            message="批量回测只能处理监控列表或全市场范围",
            retryable=False,
        )
    completion = _finalize_job(context.job_id, task_id, generation)
    frozen_entries = {entry.symbol: entry for entry in state.task.universe_snapshot}
    requested_keys = tuple(
        str(value) for value in context.config.get("item_keys", frozen_entries)
    )
    if (
        not requested_keys
        or len(requested_keys) != len(set(requested_keys))
        or any(key not in frozen_entries for key in requested_keys)
    ):
        return JobResult.failure(
            code="BACKTEST_BULK_SCOPE_INVALID",
            message="批量回测子项范围与冻结快照不一致",
            retryable=False,
        )
    entries = tuple(frozen_entries[key] for key in requested_keys)
    database = get_database()
    async with database.transaction() as session:
        jobs = JobService(session)
        item_keys = tuple(entry.symbol for entry in entries)
        await jobs.initialize_items(context.job_id, item_keys)
        for entry in entries:
            await jobs.submit(
                SubmitJob(
                    job_type="BACKTEST_SINGLE",
                    queue="bulk-backtest",
                    idempotency_scope=f"backtest-bulk-item:{context.job_id}",
                    idempotency_key=entry.symbol,
                    request_id=str(context.job_id),
                    config_snapshot={
                        "backtest_task_id": str(task_id),
                        "backtest_item_id": str(
                            uuid5(task_id, f"item:{entry.security_id}")
                        ),
                        "generation": generation,
                        "recover": bool(context.config.get("recover", False)),
                        "linked_item": {
                            "parent_job_id": str(context.job_id),
                            "item_key": entry.symbol,
                            "completion_job": _job_snapshot(completion),
                        },
                    },
                    business_object_type="backtest_item",
                    business_object_id=str(entry.security_id),
                    soft_timeout_seconds=600,
                    hard_timeout_seconds=660,
                )
            )
    return JobResult(
        success=True,
        code="CHILDREN_PENDING",
        message="批量回测逐股任务已创建",
        retryable=False,
        data={"backtest_task_id": str(task_id), "item_count": len(entries)},
    )


async def finish_linked_backtest_item(
    context: JobExecutionContext, result: JobResult
) -> JobResult:
    linked = linked_job_item(context.config)
    if linked is None or not result.success:
        return result
    status_value = str(result.data.get("status", ""))
    status = {
        "SUCCEEDED": JobItemStatus.SUCCEEDED,
        "SKIPPED": JobItemStatus.SKIPPED,
        "CANCELED": JobItemStatus.CANCELED,
        "FAILED": JobItemStatus.FAILED,
    }.get(status_value)
    task_status = str(result.data.get("task_status", ""))
    if status is None and task_status in {"PAUSED", "CANCELED"}:
        status = JobItemStatus.CANCELED
    if status is None:
        return JobResult.failure(
            code="BACKTEST_ITEM_NOT_TERMINAL",
            message="逐股回测未产生终态结果",
            retryable=True,
            data=dict(result.data),
        )
    database = get_database()
    async with database.transaction() as session:
        jobs = JobService(session)
        _completed, _total, all_terminal = await jobs.finish_item(
            child_job_id=context.job_id,
            fence_token=context.fence_token,
            parent_job_id=linked.parent_job_id,
            item_key=linked.item_key,
            status=status,
            result_ref=dict(result.data),
            error_code=(
                "BACKTEST_ITEM_FAILED"
                if status is JobItemStatus.FAILED
                else None
            ),
        )
        if all_terminal:
            await jobs.submit(linked.completion_job)
    return result


async def backtest_bulk_finalize(context: JobExecutionContext) -> JobResult:
    try:
        parent_job_id = UUID(str(context.config["parent_job_id"]))
        task_id = UUID(str(context.config["backtest_task_id"]))
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="BACKTEST_BULK_FINALIZE_CONFIG_INVALID",
            message="批量回测汇总任务配置无效",
            retryable=False,
        )
    summary = await build_backtest_application().get_summary(task_id)
    data = summary.model_dump(mode="json")
    if summary.status.value in {"PAUSED", "CANCELED"}:
        result = JobResult(
            success=True,
            code=f"BACKTEST_BATCH_{summary.status.value}",
            message=(
                "批量回测已暂停"
                if summary.status.value == "PAUSED"
                else "批量回测已取消"
            ),
            retryable=False,
            data=data,
        )
    elif summary.succeeded_items == 0:
        result = JobResult.failure(
            code="BACKTEST_BATCH_FAILED",
            message="批量回测全部失败",
            retryable=False,
            data=data,
        )
    else:
        result = JobResult(
            success=True,
            code=("SUCCESS" if summary.failed_items == 0 else "PARTIAL"),
            message=(
                "批量回测完成"
                if summary.failed_items == 0
                else "批量回测部分完成"
            ),
            retryable=False,
            data=data,
        )
    database = get_database()
    async with database.transaction() as session:
        await JobService(session).finalize_parent(parent_job_id, result)
    return result


def _finalize_job(
    parent_job_id: UUID, task_id: UUID, generation: int
) -> SubmitJob:
    return SubmitJob(
        job_type="BACKTEST_BULK_FINALIZE",
        queue="bulk-backtest",
        idempotency_scope=f"backtest-bulk-finalize:{parent_job_id}",
        idempotency_key=f"{task_id}:{generation}",
        request_id=str(parent_job_id),
        config_snapshot={
            "parent_job_id": str(parent_job_id),
            "backtest_task_id": str(task_id),
            "generation": generation,
        },
        business_object_type="backtest_task",
        business_object_id=str(task_id),
        soft_timeout_seconds=60,
        hard_timeout_seconds=120,
    )


def _job_snapshot(command: SubmitJob) -> dict[str, object]:
    return {
        "job_type": command.job_type,
        "queue": command.queue,
        "idempotency_scope": command.idempotency_scope,
        "idempotency_key": command.idempotency_key,
        "request_id": command.request_id,
        "config_snapshot": command.config_snapshot,
        "priority": command.priority,
        "business_object_type": command.business_object_type,
        "business_object_id": command.business_object_id,
        "created_by_user_id": command.created_by_user_id,
        "soft_timeout_seconds": command.soft_timeout_seconds,
        "hard_timeout_seconds": command.hard_timeout_seconds,
    }
