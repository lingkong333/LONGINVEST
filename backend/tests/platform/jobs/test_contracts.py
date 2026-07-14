from dataclasses import FrozenInstanceError

import pytest

from long_invest.platform.jobs.contracts import (
    JobItemStatus,
    JobResult,
    JobRunStatus,
    JobStatus,
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
