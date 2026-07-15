from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Mapping
from uuid import UUID

from long_invest.modules.providers.contracts import RealtimeQuote


class QuoteCycleStatus(StrEnum):
    PENDING = "PENDING"
    FETCHING = "FETCHING"
    FINALIZING = "FINALIZING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    MISSED = "MISSED"
    CANCELED = "CANCELED"


class QuoteItemStatus(StrEnum):
    VALID = "VALID"
    MISSING = "MISSING"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"
    TIMEOUT = "TIMEOUT"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    NOT_EXPECTED_TO_TRADE = "NOT_EXPECTED_TO_TRADE"


def require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone")


@dataclass(frozen=True, slots=True)
class CreateQuoteCycle:
    symbols: tuple[str, ...]
    scheduled_at: datetime
    timeout_seconds: int
    idempotency_scope: str
    idempotency_key: str
    universe_snapshot_id: str
    universe_snapshot_version: int

    def __post_init__(self) -> None:
        require_aware(self.scheduled_at)
        if not 10 <= self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 10 and 60")
        if not self.symbols:
            raise ValueError("quote scope cannot be empty")
        if len(self.symbols) > 200:
            raise ValueError("quote scope cannot exceed 200 symbols")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("quote scope contains duplicate symbols")
        if not self.idempotency_scope.strip() or not self.idempotency_key.strip():
            raise ValueError("idempotency scope and key are required")
        if self.universe_snapshot_version <= 0:
            raise ValueError("universe snapshot version must be positive")


@dataclass(frozen=True, slots=True)
class QuoteSubmission:
    symbol: str
    primary: RealtimeQuote | None = None
    fallback: RealtimeQuote | None = None
    provider_error_code: str | None = None
    not_expected_to_trade: bool = False

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol is required")
        if self.primary is None and self.fallback is None and not (
            self.provider_error_code or self.not_expected_to_trade
        ):
            raise ValueError("submission must contain a result or terminal reason")


@dataclass(frozen=True, slots=True)
class QuoteItemView:
    id: UUID
    cycle_id: UUID
    symbol: str
    status: QuoteItemStatus
    price: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    previous_close: Decimal | None
    volume: int | None
    amount: Decimal | None
    quote_time: datetime | None
    received_at: datetime | None
    provider: str | None
    error_code: str | None
    conflict_evidence: Mapping[str, object] | None
    eligible_for_evaluation: bool


@dataclass(frozen=True, slots=True)
class QuoteCycleSummary:
    id: UUID
    status: QuoteCycleStatus
    expected_count: int
    valid_count: int
    missing_count: int
    conflict_count: int
    failed_count: int
    eligible_item_ids: tuple[UUID, ...]
    eligible_symbols: tuple[str, ...]
    scheduled_at: datetime
    started_at: datetime | None
    deadline_at: datetime | None
    finalized_at: datetime | None


@dataclass(frozen=True, slots=True)
class QuoteCyclePage:
    items: tuple[QuoteCycleSummary, ...]
    total: int
    page: int
    page_size: int
