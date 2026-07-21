from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from long_invest.modules.strategies.application import get_strategy_application
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import JobExecutionContext, JobResult


@dataclass(frozen=True, slots=True)
class StrategyValidationOutcome:
    succeeded: bool
    evidence_snapshot: dict[str, Any]
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.succeeded == (self.error_code is not None):
            raise ValueError("validation result and error code are inconsistent")


class StrategyValidationExecutor(Protocol):
    async def execute(
        self, validation_run_id: UUID
    ) -> StrategyValidationOutcome: ...


_validation_executor_factory: Callable[[], StrategyValidationExecutor] | None = None


def configure_strategy_validation_executor(
    factory: Callable[[], StrategyValidationExecutor],
) -> None:
    global _validation_executor_factory
    _validation_executor_factory = factory


async def strategy_validate(context: JobExecutionContext) -> JobResult:
    try:
        validation_run_id = UUID(str(context.config["validation_run_id"]))
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="STRATEGY_VALIDATION_CONFIG_INVALID",
            message="策略验证任务缺少有效的验证编号",
            retryable=False,
        )
    if _validation_executor_factory is None:
        try:
            await get_strategy_application().record_validation_result_from_worker(
                validation_run_id,
                succeeded=False,
                error_code="STRATEGY_VALIDATION_EXECUTOR_UNAVAILABLE",
                evidence_snapshot={},
                **_worker_context(context, "记录策略验证执行器未接入"),
            )
        except AppError as exc:
            return _app_error_result(exc)
        return JobResult.failure(
            code="STRATEGY_VALIDATION_EXECUTOR_UNAVAILABLE",
            message="策略验证执行器尚未接入",
            retryable=False,
            data={"validation_run_id": str(validation_run_id)},
        )
    try:
        outcome = await _validation_executor_factory().execute(validation_run_id)
        await get_strategy_application().record_validation_result_from_worker(
            validation_run_id,
            succeeded=outcome.succeeded,
            error_code=outcome.error_code,
            evidence_snapshot=outcome.evidence_snapshot,
            **_worker_context(context, "记录策略验证结果"),
        )
    except AppError as exc:
        return _app_error_result(exc)
    return JobResult(
        success=outcome.succeeded,
        code="OK" if outcome.succeeded else str(outcome.error_code),
        message="策略验证完成" if outcome.succeeded else "策略验证失败",
        retryable=False,
        data={"validation_run_id": str(validation_run_id)},
    )


async def strategy_publish(context: JobExecutionContext) -> JobResult:
    try:
        strategy_run_id = UUID(str(context.config["strategy_run_id"]))
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="STRATEGY_PUBLISH_CONFIG_INVALID",
            message="策略发布任务缺少有效的运行编号",
            retryable=False,
        )
    try:
        version = await get_strategy_application().execute_publish(strategy_run_id)
    except AppError as exc:
        return _app_error_result(exc)
    return JobResult.success_result(
        message="策略发布完成",
        data={
            "strategy_run_id": str(strategy_run_id),
            "strategy_version_id": str(version.id),
            "status": str(version.status),
        },
    )


def _worker_context(
    context: JobExecutionContext, reason: str
) -> dict[str, str]:
    identity = f"strategy-job:{context.job_id}"
    return {
        "request_id": str(context.job_id),
        "idempotency_key": identity,
        "actor_user_id": "system:strategy-worker",
        "session_id": identity,
        "trusted_ip": "127.0.0.1",
        "reason": reason,
    }


def _app_error_result(exc: AppError) -> JobResult:
    return JobResult.failure(
        code=exc.code,
        message=exc.message,
        retryable=exc.status_code >= 500,
    )
