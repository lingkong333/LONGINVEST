import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    PENDING_DISPATCH = "PENDING_DISPATCH"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_RETRY = "WAITING_RETRY"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    LOST = "LOST"
    CANCELED = "CANCELED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"


class JobRunStatus(StrEnum):
    CLAIMED = "CLAIMED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELED = "CANCELED"
    LOST = "LOST"
    SUPERSEDED = "SUPERSEDED"


class JobItemStatus(StrEnum):
    PENDING = "PENDING"
    FETCHING = "FETCHING"
    VALIDATING = "VALIDATING"
    RUNNING = "RUNNING"
    SAVING = "SAVING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELED = "CANCELED"


@dataclass(frozen=True, slots=True)
class JobResult:
    success: bool
    code: str
    message: str
    retryable: bool
    data: Any = None
    warnings: tuple[str, ...] = ()
    metrics: dict[str, int | float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            json.dumps(self.as_dict(), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("job result must be JSON-compatible") from exc

    @classmethod
    def success_result(
        cls,
        *,
        data: Any = None,
        message: str = "任务执行成功",
        warnings: tuple[str, ...] = (),
        metrics: dict[str, int | float] | None = None,
    ) -> "JobResult":
        return cls(
            success=True,
            code="OK",
            message=message,
            retryable=False,
            data=data,
            warnings=warnings,
            metrics=metrics or {},
        )

    @classmethod
    def failure(
        cls,
        *,
        code: str,
        message: str,
        retryable: bool,
        data: Any = None,
        warnings: tuple[str, ...] = (),
        metrics: dict[str, int | float] | None = None,
    ) -> "JobResult":
        return cls(
            success=False,
            code=code,
            message=message,
            retryable=retryable,
            data=data,
            warnings=warnings,
            metrics=metrics or {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "data": self.data,
            "warnings": list(self.warnings),
            "metrics": self.metrics,
        }


@dataclass(frozen=True, slots=True)
class SubmitJob:
    job_type: str
    queue: str
    idempotency_scope: str
    idempotency_key: str
    request_id: str
    config_snapshot: dict[str, Any]
    priority: int = 0
    business_object_type: str | None = None
    business_object_id: str | None = None
    created_by_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobProgress:
    completed: int
    total: int
    message: str | None = None

    def __post_init__(self) -> None:
        if self.completed < 0 or self.total < 0 or self.completed > self.total:
            raise ValueError("job progress is outside the valid range")
