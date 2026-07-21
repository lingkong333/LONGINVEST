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
    application = get_strategy_application()
    try:
        run = await application.get_validation_run(validation_run_id)
    except AppError as exc:
        return _app_error_result(exc)
    terminal = _terminal_validation_result(run)
    if terminal is not None:
        return terminal
    if str(run.status) != "PENDING":
        return await _settle_validation_failure(
            application,
            validation_run_id,
            context,
            code="STRATEGY_VALIDATION_STATE_INVALID",
            reason="结束不可执行的策略验证状态",
        )
    if _validation_executor_factory is None:
        return await _settle_validation_failure(
            application,
            validation_run_id,
            context,
            code="STRATEGY_VALIDATION_EXECUTOR_UNAVAILABLE",
            reason="记录策略验证执行器未接入",
        )
    try:
        outcome = await _validation_executor_factory().execute(validation_run_id)
    except Exception:
        return await _settle_validation_failure(
            application,
            validation_run_id,
            context,
            code="STRATEGY_VALIDATION_EXECUTION_FAILED",
            reason="记录策略验证执行异常",
        )
    try:
        completed = await application.record_validation_result_from_worker(
            validation_run_id,
            succeeded=outcome.succeeded,
            error_code=outcome.error_code,
            evidence_snapshot=outcome.evidence_snapshot,
            **_worker_context(context, "记录策略验证结果"),
        )
    except Exception:
        return await _recover_validation_confirmation(
            application,
            validation_run_id,
            context,
        )
    result = _terminal_validation_result(completed)
    if result is None:
        return await _settle_validation_failure(
            application,
            validation_run_id,
            context,
            code="STRATEGY_VALIDATION_STATE_INVALID",
            reason="结束未完成的策略验证结果",
        )
    return result


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
    except Exception:
        return JobResult.failure(
            code="STRATEGY_PUBLISH_EXECUTION_FAILED",
            message="策略发布执行失败，请使用新的请求编号重试",
            retryable=False,
        )
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
        retryable=False,
    )


def _terminal_validation_result(run: Any) -> JobResult | None:
    run_id = str(run.id)
    status = str(run.status)
    if status == "SUCCEEDED":
        return JobResult.success_result(
            message="策略验证完成",
            data={"validation_run_id": run_id, "replayed": True},
        )
    if status == "FAILED":
        return JobResult.failure(
            code=str(run.error_code or "STRATEGY_VALIDATION_FAILED"),
            message="策略验证失败",
            retryable=False,
            data={"validation_run_id": run_id, "replayed": True},
        )
    return None


async def _settle_validation_failure(
    application: Any,
    validation_run_id: UUID,
    context: JobExecutionContext,
    *,
    code: str,
    reason: str,
) -> JobResult:
    try:
        completed = await application.record_validation_result_from_worker(
            validation_run_id,
            succeeded=False,
            error_code=code,
            evidence_snapshot={},
            **_worker_context(context, reason),
        )
    except Exception:
        return await _recover_validation_confirmation(
            application,
            validation_run_id,
            context,
        )
    result = _terminal_validation_result(completed)
    return result or JobResult.failure(
        code="STRATEGY_VALIDATION_STATE_UNCERTAIN",
        message="策略验证状态暂时无法确认",
        retryable=False,
    )


async def _recover_validation_confirmation(
    application: Any,
    validation_run_id: UUID,
    context: JobExecutionContext,
) -> JobResult:
    try:
        current = await application.get_validation_run(validation_run_id)
    except Exception:
        return JobResult.failure(
            code="STRATEGY_VALIDATION_STATE_UNCERTAIN",
            message="策略验证状态暂时无法确认",
            retryable=False,
        )
    terminal = _terminal_validation_result(current)
    if terminal is not None:
        return terminal
    try:
        failed = await application.record_validation_result_from_worker(
            validation_run_id,
            succeeded=False,
            error_code="STRATEGY_VALIDATION_CONFIRMATION_FAILED",
            evidence_snapshot={},
            **_worker_context(context, "收敛无法确认的策略验证结果"),
        )
    except Exception:
        return JobResult.failure(
            code="STRATEGY_VALIDATION_STATE_UNCERTAIN",
            message="策略验证状态暂时无法确认",
            retryable=False,
        )
    return _terminal_validation_result(failed) or JobResult.failure(
        code="STRATEGY_VALIDATION_STATE_UNCERTAIN",
        message="策略验证状态暂时无法确认",
        retryable=False,
    )
