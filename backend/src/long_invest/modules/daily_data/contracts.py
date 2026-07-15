from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from long_invest.modules.providers.contracts import validate_symbol


class DailyBatchStatus(StrEnum):
    PENDING = "PENDING"
    FETCHING = "FETCHING"
    VALIDATING = "VALIDATING"
    COMMITTING = "COMMITTING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DailyStageStatus(StrEnum):
    FETCHED = "FETCHED"
    VALID = "VALID"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INVALID = "INVALID"
    MISSING = "MISSING"
    FAILED = "FAILED"


class DailyMissingReason(StrEnum):
    SUSPENDED = "SUSPENDED"
    NOT_YET_LISTED = "NOT_YET_LISTED"
    DELISTED = "DELISTED"
    NOT_EXPECTED_TO_TRADE = "NOT_EXPECTED_TO_TRADE"
    UNEXPLAINED = "UNEXPLAINED"

    @property
    def explained(self) -> bool:
        return self is not DailyMissingReason.UNEXPLAINED


def _require_uuid(value: UUID | None, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"{field_name}必须是有效 UUID")
    return value


def _require_date(value: date, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{field_name}必须是有效日期")
    return value


def _require_aware(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name}必须包含时区")
    return value


def _require_idempotency_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError("幂等键必须为 1 到 160 个字符")
    return value.strip()


@dataclass(frozen=True, slots=True)
class CreateDailyBatch:
    trading_date: date
    universe_snapshot_id: UUID | None
    symbols: tuple[str, ...]
    idempotency_key: str
    parent_batch_id: UUID | None = None
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_date(self.trading_date, "目标日期")
        _require_uuid(self.universe_snapshot_id, "范围快照编号")
        symbols = tuple(self.symbols)
        if not symbols:
            raise ValueError("冻结股票范围不能为空")
        for symbol in symbols:
            validate_symbol(symbol)
        if len(symbols) != len(set(symbols)):
            raise ValueError("冻结股票范围不能包含重复代码")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(
            self, "idempotency_key", _require_idempotency_key(self.idempotency_key)
        )
        if self.parent_batch_id is not None:
            _require_uuid(self.parent_batch_id, "原批次编号")
        if self.deadline_at is not None:
            _require_aware(self.deadline_at, "截止时间")


@dataclass(frozen=True, slots=True)
class DailyRetryAuditContext:
    request_id: str
    idempotency_key: str
    actor_user_id: str
    session_id: str
    trusted_ip: str
    reason: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.request_id, "请求编号"),
            (self.actor_user_id, "操作用户"),
            (self.session_id, "会话编号"),
            (self.trusted_ip, "可信来源地址"),
            (self.reason, "重试原因"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name}不能为空")
        object.__setattr__(
            self, "idempotency_key", _require_idempotency_key(self.idempotency_key)
        )
        if len(self.reason) > 500:
            raise ValueError("重试原因不能超过 500 个字符")


@dataclass(frozen=True, slots=True)
class StageDailyBar:
    symbol: str
    security_id: UUID
    trading_date: date
    status: DailyStageStatus
    received_at: datetime
    provider_payload: Mapping[str, Any] | None = None
    missing_reason: DailyMissingReason | None = None
    error_code: str | None = None
    quality_code: str | None = None

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        _require_uuid(self.security_id, "股票编号")
        _require_date(self.trading_date, "交易日期")
        _require_aware(self.received_at, "接收时间")
        try:
            status = DailyStageStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("不支持的暂存状态") from exc
        object.__setattr__(self, "status", status)
        reason = self.missing_reason
        if reason is not None:
            try:
                reason = DailyMissingReason(reason)
            except (TypeError, ValueError) as exc:
                raise ValueError("不支持的缺失原因") from exc
            object.__setattr__(self, "missing_reason", reason)
        if status is DailyStageStatus.MISSING and reason is None:
            raise ValueError("缺失状态必须提供明确缺失原因")
        if status is not DailyStageStatus.MISSING and reason is not None:
            raise ValueError("只有缺失状态可以提供缺失原因")
        if (
            status
            in {
                DailyStageStatus.FETCHED,
                DailyStageStatus.VALID,
                DailyStageStatus.REVIEW_REQUIRED,
            }
            and not self.provider_payload
        ):
            raise ValueError("有效暂存状态必须提供日线数据")
        if self.provider_payload is not None:
            object.__setattr__(
                self, "provider_payload", MappingProxyType(dict(self.provider_payload))
            )


@dataclass(frozen=True, slots=True)
class DailyBatchSummary:
    id: UUID
    trading_date: date
    universe_snapshot_id: UUID
    status: DailyBatchStatus
    expected_count: int
    fetched_count: int = 0
    validated_count: int = 0
    committed_count: int = 0
    missing_count: int = 0
    failed_count: int = 0
    created_at: datetime | None = None
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.id, "批次编号")
        _require_uuid(self.universe_snapshot_id, "范围快照编号")
        _require_date(self.trading_date, "目标日期")
        object.__setattr__(self, "status", DailyBatchStatus(self.status))
        counts = (
            self.expected_count,
            self.fetched_count,
            self.validated_count,
            self.committed_count,
            self.missing_count,
            self.failed_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("批次数量不能为负数")
        for value in (
            self.created_at,
            self.started_at,
            self.deadline_at,
            self.completed_at,
        ):
            if value is not None:
                _require_aware(value, "批次时间")


@dataclass(frozen=True, slots=True)
class DailyBarView:
    security_id: UUID
    symbol: str
    trade_date: date
    open: str
    high: str
    low: str
    close: str
    previous_close: str | None
    volume: int
    amount: str
    source: str
    data_version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DailyRevisionView:
    id: UUID
    security_id: UUID
    trade_date: date
    revision_no: int
    old_values: Mapping[str, Any]
    new_values: Mapping[str, Any]
    changed_fields: tuple[str, ...]
    source: str
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DailyMissingView:
    batch_id: UUID
    symbol: str
    security_id: UUID
    reason: DailyMissingReason
    error_code: str | None
    explained: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...] = field(default_factory=tuple)
    total: int = 0
    page: int = 1
    page_size: int = 50

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if self.total < 0 or self.page < 1 or self.page_size < 1:
            raise ValueError("分页参数无效")
