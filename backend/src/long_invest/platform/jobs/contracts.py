import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID


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


TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
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
    soft_timeout_seconds: int = 30
    hard_timeout_seconds: int = 60

    def __post_init__(self) -> None:
        if not 0 < self.soft_timeout_seconds <= self.hard_timeout_seconds <= 3600:
            raise ValueError("job timeout must satisfy 0 < soft <= hard <= 3600")
        try:
            snapshot = json.loads(
                json.dumps(self.config_snapshot, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("job config snapshot must be JSON-compatible") from exc
        if not isinstance(snapshot, dict):
            raise ValueError("job config snapshot must be an object")
        object.__setattr__(self, "config_snapshot", snapshot)


@dataclass(frozen=True, slots=True)
class LinkedJobItem:
    parent_job_id: UUID
    item_key: str
    completion_job: SubmitJob


def linked_job_item(config: Mapping[str, Any]) -> LinkedJobItem | None:
    value = config.get("linked_item")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("linked_item must be an object")
    completion = value.get("completion_job")
    if not isinstance(completion, Mapping):
        raise ValueError("linked item completion job must be an object")
    return LinkedJobItem(
        parent_job_id=UUID(str(value["parent_job_id"])),
        item_key=str(value["item_key"]),
        completion_job=SubmitJob(**dict(completion)),
    )


def linked_parent_job_id(config: Mapping[str, Any]) -> UUID | None:
    value = config.get("linked_parent_job_id")
    return UUID(str(value)) if value is not None else None


@dataclass(frozen=True, slots=True)
class JobExecutionContext:
    job_id: UUID
    fence_token: UUID
    config: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", _freeze_mapping(self.config))


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class JobProgress:
    completed: int
    total: int
    message: str | None = None

    def __post_init__(self) -> None:
        if self.completed < 0 or self.total < 0 or self.completed > self.total:
            raise ValueError("job progress is outside the valid range")
