from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import JobExecutionContext, JobResult


def _default_application():
    from long_invest.modules.targets.api import get_target_application

    return get_target_application()


_application_factory: Callable[[], Any] = _default_application


def configure_target_job_application(factory: Callable[[], Any]) -> None:
    global _application_factory
    _application_factory = factory


async def target_calculate(context: JobExecutionContext) -> JobResult:
    try:
        if set(context.config) != {"run_id"}:
            raise ValueError("target job config only accepts run_id")
        run_id = UUID(str(context.config["run_id"]))
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="TARGET_CALCULATE_CONFIG_INVALID",
            message="目标计算任务配置无效",
            retryable=False,
        )
    try:
        result = await _application_factory().execute(run_id)
    except AppError as exc:
        return JobResult.failure(
            code=exc.code,
            message=exc.message,
            retryable=exc.status_code >= 500,
            data={"run_id": str(run_id)},
        )
    data = {
        "run_id": str(result.run_id),
        "revision_id": str(result.revision_id) if result.revision_id else None,
        "review_id": str(result.review_id) if result.review_id else None,
        "replayed": result.replayed,
    }
    if result.code == "TARGET_CALCULATION_FAILED":
        return JobResult.failure(
            code=result.code,
            message="目标计算失败",
            retryable=False,
            data=data,
        )
    return JobResult(
        success=True,
        code=result.code,
        message="目标计算完成",
        retryable=False,
        data=data,
    )
