from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from uuid import UUID


class QfqDatasetLifecycle(StrEnum):
    STAGING = "STAGING"
    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"


class QfqFreshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"


class QfqRefreshStatus(StrEnum):
    PENDING = "PENDING"
    FETCHING = "FETCHING"
    VALIDATING = "VALIDATING"
    COMMITTING = "COMMITTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    SUPERSEDED = "SUPERSEDED"


class QfqValidationError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")


def _require_symbol(value: str) -> str:
    if not isinstance(value, str) or not _SYMBOL_PATTERN.fullmatch(value):
        raise ValueError("symbol must use the unified 600000.SH/000001.SZ form")
    return value


def _require_date(value: date, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a date")
    return value


def _require_nonblank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


def _require_decimal(value: Decimal, field_name: str, *, positive: bool) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    try:
        valid = value > 0 if positive else value >= 0
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is invalid") from exc
    if not valid:
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{field_name} must be {qualifier}")
    return value


@dataclass(frozen=True, slots=True)
class RefreshQfq:
    security_id: UUID
    symbol: str
    start: date
    end: date
    as_of_date: date
    input_daily_version: int
    trigger_reason: str
    request_id: str
    idempotency_key: str
    actor_user_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.security_id, UUID):
            raise ValueError("security_id must be a UUID")
        _require_symbol(self.symbol)
        _require_date(self.start, "start")
        _require_date(self.end, "end")
        _require_date(self.as_of_date, "as_of_date")
        if self.start > self.as_of_date or self.as_of_date != self.end:
            raise ValueError("window must satisfy start <= as_of_date == end")
        if (
            not isinstance(self.input_daily_version, int)
            or isinstance(self.input_daily_version, bool)
            or self.input_daily_version <= 0
        ):
            raise ValueError("input_daily_version must be a positive version")
        for field_name in (
            "trigger_reason",
            "request_id",
            "idempotency_key",
            "actor_user_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_nonblank(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class QfqBarInput:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal

    def __post_init__(self) -> None:
        _require_date(self.trade_date, "trade_date")
        for field_name in ("open", "high", "low", "close"):
            _require_decimal(getattr(self, field_name), field_name, positive=True)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be the greatest OHLC price")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be the smallest OHLC price")
        if (
            not isinstance(self.volume, int)
            or isinstance(self.volume, bool)
            or self.volume < 0
        ):
            raise ValueError("volume must be a nonnegative integer")
        _require_decimal(self.amount, "amount", positive=False)


@dataclass(frozen=True, slots=True)
class ValidatedQfqWindow:
    bars: tuple[QfqBarInput, ...]
    anchor_date: date
    anchor_close: Decimal
    row_count: int
    checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", tuple(self.bars))


@dataclass(frozen=True, slots=True)
class QfqBarView:
    trade_date: date
    open: str
    high: str
    low: str
    close: str
    volume: int
    amount: str


@dataclass(frozen=True, slots=True)
class QfqDatasetView:
    id: UUID
    security_id: UUID
    symbol: str
    version: int
    requested_start: date
    requested_end: date
    actual_start: date
    actual_end: date
    as_of_date: date
    provider: str
    provider_contract_version: str
    anchor_date: date
    anchor_close: str
    row_count: int
    checksum: str
    lifecycle: QfqDatasetLifecycle
    freshness: QfqFreshness
    stale_reason: str | None
    created_at: datetime
    activated_at: datetime | None
    superseded_at: datetime | None


@dataclass(frozen=True, slots=True)
class QfqRefreshView:
    id: UUID
    job_id: UUID
    security_id: UUID
    symbol: str
    start: date
    end: date
    as_of_date: date
    input_daily_version: int
    status: QfqRefreshStatus
    candidate_dataset_id: UUID | None
    activated_dataset_id: UUID | None
    row_count: int | None
    checksum: str | None
    error_code: str | None
    retryable: bool | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...] = field(default_factory=tuple)
    total: int = 0
    page: int = 1
    page_size: int = 50

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if self.total < 0 or self.page < 1 or self.page_size < 1:
            raise ValueError("invalid pagination")

