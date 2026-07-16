from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from long_invest.platform.jobs import contracts as job_contracts
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobItemStatus,
    JobResult,
    JobRunStatus,
    JobStatus,
    SubmitJob,
)


def test_terminal_job_statuses_have_one_shared_contract() -> None:
    assert (
        frozenset(
            {
                JobStatus.SUCCEEDED,
                JobStatus.PARTIAL,
                JobStatus.FAILED,
                JobStatus.TIMED_OUT,
                JobStatus.LOST,
                JobStatus.CANCELED,
                JobStatus.REJECTED,
            }
        )
        == job_contracts.TERMINAL_JOB_STATUSES
    )


def test_job_statuses_match_v31_contract() -> None:
    assert {status.value for status in JobStatus} == {
        "PENDING_DISPATCH",
        "QUEUED",
        "RUNNING",
        "WAITING_RETRY",
        "PAUSING",
        "PAUSED",
        "CANCEL_REQUESTED",
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "TIMED_OUT",
        "LOST",
        "CANCELED",
        "BLOCKED",
        "REJECTED",
    }
    assert {status.value for status in JobRunStatus} == {
        "CLAIMED",
        "STARTING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "TIMED_OUT",
        "CANCELED",
        "LOST",
        "SUPERSEDED",
    }
    assert {status.value for status in JobItemStatus} == {
        "PENDING",
        "FETCHING",
        "VALIDATING",
        "RUNNING",
        "SAVING",
        "SUCCEEDED",
        "FAILED",
        "SKIPPED",
        "CANCELED",
    }


def test_job_result_is_immutable_and_json_safe() -> None:
    result = JobResult.failure(
        code="PROVIDER_TIMEOUT",
        message="行情数据源响应超时",
        retryable=True,
        warnings=("已切换备用源",),
        metrics={"duration_ms": 12_000},
    )

    assert result.as_dict() == {
        "success": False,
        "code": "PROVIDER_TIMEOUT",
        "message": "行情数据源响应超时",
        "retryable": True,
        "data": None,
        "warnings": ["已切换备用源"],
        "metrics": {"duration_ms": 12_000},
    }
    with pytest.raises(FrozenInstanceError):
        result.code = "CHANGED"  # type: ignore[misc]


def test_job_result_rejects_exception_objects() -> None:
    with pytest.raises(TypeError, match="JSON-compatible"):
        JobResult.failure(
            code="UNSAFE",
            message="不安全结果",
            retryable=False,
            data={"error": RuntimeError("secret")},
        )


def test_submit_job_freezes_supported_timeout_range() -> None:
    command = SubmitJob(
        job_type="REALTIME_QUOTE_CYCLE",
        queue="realtime-quotes",
        idempotency_scope="quote:2026-07-16T10:00",
        idempotency_key="first",
        request_id="req-timeout",
        config_snapshot={"symbols": ["600000.SH"]},
        soft_timeout_seconds=45,
        hard_timeout_seconds=60,
    )

    assert command.soft_timeout_seconds == 45
    assert command.hard_timeout_seconds == 60

    for soft, hard in ((0, 60), (61, 60), (45, 3601)):
        with pytest.raises(ValueError, match="timeout"):
            SubmitJob(
                job_type="REALTIME_QUOTE_CYCLE",
                queue="realtime-quotes",
                idempotency_scope="quote:invalid",
                idempotency_key=f"{soft}:{hard}",
                request_id="req-invalid-timeout",
                config_snapshot={},
                soft_timeout_seconds=soft,
                hard_timeout_seconds=hard,
            )


def test_execution_context_exposes_read_only_frozen_config() -> None:
    config = {"symbols": ["600000.SH"]}
    context = JobExecutionContext(
        job_id=uuid4(),
        fence_token=uuid4(),
        config=config,
    )
    config["changed"] = True
    config["symbols"].append("000001.SZ")

    assert "changed" not in context.config
    assert context.config["symbols"] == ("600000.SH",)
    with pytest.raises(TypeError):
        context.config["changed"] = True  # type: ignore[index]
    with pytest.raises(AttributeError):
        context.config["symbols"].append("000001.SZ")
