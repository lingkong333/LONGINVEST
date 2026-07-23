from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DecimalException
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from long_invest.modules.targets.contracts import TargetValues
from long_invest.platform.json_snapshot import freeze_json_mapping, thaw_json_value
from long_invest.platform.validation import Sha256Hex


@dataclass(frozen=True, slots=True)
class ValidationEvidenceClaim:
    validation_run_id: UUID
    strategy_id: UUID
    draft_version: int
    source_code_hash: str
    metadata_hash: str
    parameter_schema_hash: str
    parameter_hash: str
    environment_hash: str
    runner_image_digest: str
    checks: Mapping[str, Mapping[str, Any]]


class ValidationEvidenceVerifier(Protocol):
    """Re-query owner modules for every referenced run, task, and snapshot."""

    async def verify(self, claim: ValidationEvidenceClaim) -> bool: ...


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyForecastErrorCode(StrEnum):
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    TRAINING_DATA_INVALID = "TRAINING_DATA_INVALID"
    STRATEGY_FORECAST_TIMEOUT = "STRATEGY_FORECAST_TIMEOUT"
    STRATEGY_TARGET_INVALID = "STRATEGY_TARGET_INVALID"
    TEST_DATA_EXPOSED_TO_STRATEGY = "TEST_DATA_EXPOSED_TO_STRATEGY"


class StrategyReadinessStatus(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    ARCHIVED = "ARCHIVED"


class StrategyLifecycleStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    ARCHIVED = "ARCHIVED"


class StrategyAction(StrEnum):
    RENAME = "RENAME"
    SAVE_DRAFT = "SAVE_DRAFT"
    CREATE_REVISION = "CREATE_REVISION"
    RESTORE_REVISION = "RESTORE_REVISION"
    VALIDATE = "VALIDATE"
    TEST = "TEST"
    PUBLISH = "PUBLISH"
    ARCHIVE = "ARCHIVE"
    RESTORE = "RESTORE"


class ValidationRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class StrategyRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class StrategyVersionOperation(StrEnum):
    APPLY = "APPLY"
    ROLLBACK = "ROLLBACK"


class StrategySubscriptionScope(StrEnum):
    SELECTED = "SELECTED"
    ALL_RELATED = "ALL_RELATED"


class StrategyOperationItemStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REUSED = "REUSED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class StrategyLifecycleErrorCode(StrEnum):
    STRATEGY_VERSION_CONFLICT = "STRATEGY_VERSION_CONFLICT"
    STRATEGY_NOT_READY = "STRATEGY_NOT_READY"
    STRATEGY_VALIDATION_REQUIRED = "STRATEGY_VALIDATION_REQUIRED"
    STRATEGY_VALIDATION_STALE = "STRATEGY_VALIDATION_STALE"
    STRATEGY_PUBLISH_IN_PROGRESS = "STRATEGY_PUBLISH_IN_PROGRESS"
    STRATEGY_PUBLISH_FAILED = "STRATEGY_PUBLISH_FAILED"
    STRATEGY_VERSION_IMMUTABLE = "STRATEGY_VERSION_IMMUTABLE"
    STRATEGY_ARCHIVED = "STRATEGY_ARCHIVED"


class FrozenMappingContract(StrictContract):
    parameter_snapshot: Mapping[str, Any]

    @field_validator("parameter_snapshot")
    @classmethod
    def freeze_parameter_snapshot(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json_mapping(value)

    @field_serializer("parameter_snapshot")
    def serialize_parameter_snapshot(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)


class TrainingDataSnapshot(StrictContract):
    security_id: UUID
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    start_date: date
    end_date: date
    data_version: int = Field(ge=1)
    fetched_at: AwareDatetime
    source: str = Field(min_length=1, max_length=64)
    price_basis: str = Field(min_length=1, max_length=32)
    content_hash: Sha256Hex
    rows: tuple[Mapping[str, Any], ...]

    @field_validator("rows")
    @classmethod
    def freeze_rows(
        cls, value: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(freeze_json_mapping(row) for row in value)

    @field_serializer("rows")
    def serialize_rows(self, value: tuple[Mapping[str, Any], ...]) -> list[Any]:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def validate_rows(self) -> TrainingDataSnapshot:
        if not self.rows:
            raise ValueError("training rows must not be empty")
        dates = [row.get("trade_date") for row in self.rows]
        if any(not isinstance(value, date) for value in dates):
            raise ValueError("training rows require trade_date")
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ValueError("training rows must be strictly ordered")
        if dates[0] < self.start_date or dates[-1] > self.end_date:
            raise ValueError("training rows exceed requested range")
        for row in self.rows:
            try:
                low, open_, close, high = (
                    Decimal(str(row[key])) for key in ("low", "open", "close", "high")
                )
            except (KeyError, ValueError, DecimalException) as exc:
                raise ValueError("training rows require OHLC") from exc
            if (
                any(
                    not value.is_finite() or value <= 0
                    for value in (low, open_, close, high)
                )
                or low > min(open_, close)
                or high < max(open_, close, low)
            ):
                raise ValueError("training row OHLC is inconsistent")
        return self


class StrategyForecastRequest(FrozenMappingContract):
    strategy_id: UUID
    security_name: str = Field(min_length=1, max_length=100)
    strategy_version_id: UUID | None = None
    draft_id: UUID | None = None
    draft_version: int | None = Field(default=None, ge=1)
    source_code: str = Field(min_length=1)
    source_code_hash: Sha256Hex
    metadata: Mapping[str, Any]
    parameter_schema: Mapping[str, Any]
    environment_version: str = Field(min_length=1)
    runner_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parameter_hash: Sha256Hex
    training_data: TrainingDataSnapshot
    requested_at: AwareDatetime

    @field_validator("metadata", "parameter_schema")
    @classmethod
    def freeze_strategy_mapping(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json_mapping(value)

    @field_serializer("metadata", "parameter_schema")
    def serialize_strategy_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def validate_strategy_snapshot(self) -> StrategyForecastRequest:
        has_version = self.strategy_version_id is not None
        has_draft = self.draft_id is not None or self.draft_version is not None
        if has_version == has_draft:
            raise ValueError("choose one published strategy version or frozen draft")
        if has_draft and (self.draft_id is None or self.draft_version is None):
            raise ValueError("frozen draft requires id and version")
        return self


class StrategyForecastResult(StrictContract):
    values: TargetValues
    diagnostics: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("diagnostics")
    @classmethod
    def freeze_diagnostics(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json_mapping(value)

    @field_serializer("diagnostics")
    def serialize_diagnostics(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)


class StrategyReadiness(StrictContract):
    strategy_version_id: UUID
    status: StrategyReadinessStatus
    checked_at: AwareDatetime
    failure_code: StrategyForecastErrorCode | None = None


class StrategyView(StrictContract):
    id: UUID
    name: str = Field(min_length=1)
    status: StrategyLifecycleStatus


class StrategyDraftView(StrictContract):
    id: UUID
    strategy_id: UUID
    draft_version: int = Field(ge=1)
    source_code: str


class StrategyDraftRevisionView(StrategyDraftView):
    revision_no: int = Field(ge=1)


class StrategyVersionView(StrictContract):
    id: UUID
    strategy_id: UUID
    version_no: int = Field(ge=1)
    source_code: str = Field(min_length=1)
    metadata: Mapping[str, Any]
    parameter_schema: Mapping[str, Any]
    environment_version: str = Field(min_length=1)
    runner_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_code_hash: Sha256Hex
    git_commit: str | None = Field(default=None, min_length=7, max_length=64)
    validation_run_id: UUID | None = None
    status: StrategyLifecycleStatus
    published_at: AwareDatetime | None = None
    created_at: AwareDatetime

    @field_validator("metadata", "parameter_schema")
    @classmethod
    def freeze_release_mapping(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json_mapping(value)

    @field_serializer("metadata", "parameter_schema")
    def serialize_release_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def validate_publication_fields(self) -> StrategyVersionView:
        version_statuses = {
            StrategyLifecycleStatus.PUBLISHING,
            StrategyLifecycleStatus.PUBLISHED,
            StrategyLifecycleStatus.PUBLISH_FAILED,
            StrategyLifecycleStatus.ARCHIVED,
        }
        if self.status not in version_statuses:
            raise ValueError("strategy version has an invalid lifecycle status")
        publication_fields = (
            self.git_commit,
            self.validation_run_id,
            self.published_at,
        )
        if self.status in {
            StrategyLifecycleStatus.PUBLISHED,
            StrategyLifecycleStatus.ARCHIVED,
        } and any(value is None for value in publication_fields):
            raise ValueError("published strategy version requires publication fields")
        if (
            self.status
            in {
                StrategyLifecycleStatus.PUBLISHING,
                StrategyLifecycleStatus.PUBLISH_FAILED,
            }
            and self.published_at is not None
        ):
            raise ValueError("unpublished strategy version cannot have published_at")
        return self


class StrategyValidationRunView(StrictContract):
    id: UUID
    strategy_id: UUID
    strategy_version_id: UUID | None = None
    draft_version: int = Field(ge=1)
    source_code_hash: Sha256Hex
    evidence_snapshot: Mapping[str, Any]
    status: ValidationRunStatus
    error_code: StrategyLifecycleErrorCode | None = None
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @field_validator("evidence_snapshot")
    @classmethod
    def freeze_evidence_snapshot(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json_mapping(value)

    @field_serializer("evidence_snapshot")
    def serialize_evidence_snapshot(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def validate_completion(self) -> StrategyValidationRunView:
        is_complete = self.status in {
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
        }
        if is_complete != (self.completed_at is not None):
            raise ValueError("completed_at must match validation status")
        if (self.status is ValidationRunStatus.FAILED) != (self.error_code is not None):
            raise ValueError("error code must match failed validation status")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("validation cannot complete before it starts")
        return self


class StrategyRunView(StrictContract):
    id: UUID
    strategy_version_id: UUID
    status: StrategyRunStatus


class StrategyStockTestRequest(FrozenMappingContract):
    strategy_id: UUID
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    training_start_date: date
    training_end_date: date
    test_start_date: date
    test_end_date: date
    initial_capital: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_date_range(self) -> StrategyStockTestRequest:
        if not (
            self.training_start_date
            <= self.training_end_date
            < self.test_start_date
            <= self.test_end_date
        ):
            raise ValueError("training and test ranges must not overlap")
        return self


class StrategyStockTestSubmission(StrictContract):
    task_id: UUID
    status: str = Field(min_length=1, max_length=32)
    replayed: bool = False


class StrategySubscriptionCandidate(FrozenMappingContract):
    subscription_id: UUID
    subscription_version: int = Field(ge=1)
    target_version: int = Field(ge=1)


class StrategyVersionTargetRequest(FrozenMappingContract):
    operation: StrategyVersionOperation
    strategy_id: UUID
    strategy_version_id: UUID
    subscription_id: UUID
    subscription_version: int = Field(ge=1)
    target_version: int = Field(ge=1)
    target_date: date
    training_start_date: date
    training_end_date: date
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=64)
    actor_user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    trusted_ip: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_training_range(self) -> StrategyVersionTargetRequest:
        if self.training_start_date > self.training_end_date:
            raise ValueError("training range is invalid")
        return self


class StrategyVersionTargetSubmission(StrictContract):
    code: str = Field(min_length=1, max_length=100)
    run_id: UUID
    job_id: UUID
    replayed: bool = False


class StrategyOperationItemResult(StrictContract):
    subscription_id: UUID
    status: StrategyOperationItemStatus
    code: str = Field(min_length=1, max_length=100)
    run_id: UUID | None = None
    job_id: UUID | None = None


class StrategyOperationBatchResult(StrictContract):
    operation: StrategyVersionOperation
    strategy_id: UUID
    strategy_version_id: UUID
    replayed: bool
    items: tuple[StrategyOperationItemResult, ...]


class StrategyStockTestPort(Protocol):
    async def submit_strategy_test(
        self,
        *,
        task_id: UUID,
        draft: StrategyDraftView,
        metadata: Mapping[str, Any],
        parameter_schema: Mapping[str, Any],
        request: StrategyStockTestRequest,
        idempotency_key: str,
        request_id: str,
        actor_user_id: str,
        reason: str,
    ) -> StrategyStockTestSubmission: ...


class StrategySubscriptionScopePort(Protocol):
    async def resolve_strategy_subscriptions(
        self,
        *,
        strategy_id: UUID,
        scope: StrategySubscriptionScope,
        subscription_ids: tuple[UUID, ...],
    ) -> tuple[StrategySubscriptionCandidate, ...]: ...


class StrategyVersionTargetPort(Protocol):
    async def submit_strategy_version_target(
        self, request: StrategyVersionTargetRequest
    ) -> StrategyVersionTargetSubmission: ...


class StrategyForecastPort(Protocol):
    async def forecast(
        self, request: StrategyForecastRequest
    ) -> StrategyForecastResult: ...


class TestDataPort(Protocol):
    async def get_test_data(
        self,
        *,
        security_id: UUID,
        start_date: date,
        end_date: date,
    ) -> TrainingDataSnapshot | None: ...


class TrainingDataPort(Protocol):
    async def get_training_data(
        self,
        *,
        security_id: UUID,
        start_date: date,
        end_date: date,
    ) -> TrainingDataSnapshot | None: ...


class StrategyReadinessPort(Protocol):
    async def get_strategy_readiness(
        self, strategy_version_id: UUID
    ) -> StrategyReadiness | None: ...


class StrategyExecutionSnapshotPort(Protocol):
    async def get_execution_snapshot(
        self, strategy_version_id: UUID
    ) -> StrategyVersionView | None: ...


class StrategyForecastRequestVerifier(Protocol):
    async def verify_forecast_request(
        self, request: StrategyForecastRequest
    ) -> bool: ...
