from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from math import ceil
from typing import Protocol


class ProviderCapability(StrEnum):
    SECURITY_MASTER = "SECURITY_MASTER"
    REALTIME_QUOTE_BATCH = "REALTIME_QUOTE_BATCH"
    DAILY_BAR_UNADJUSTED = "DAILY_BAR_UNADJUSTED"
    HISTORICAL_DAILY_UNADJUSTED = "HISTORICAL_DAILY_UNADJUSTED"
    HISTORICAL_DAILY_QFQ = "HISTORICAL_DAILY_QFQ"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"


class ProviderCode(StrEnum):
    EASTMONEY = "EASTMONEY"
    SINA = "SINA"
    TUSHARE = "TUSHARE"
    BAOSTOCK = "BAOSTOCK"
    TENCENT = "TENCENT"


class ProviderAdapterCode(StrEnum):
    HTTPX = "HTTPX"
    AKSHARE = "AKSHARE"
    TUSHARE_SDK = "TUSHARE_SDK"
    BAOSTOCK_SDK = "BAOSTOCK_SDK"


class DailyCollectionMode(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    PAGED = "PAGED"
    BATCHED_SYMBOLS = "BATCHED_SYMBOLS"
    SINGLE_SYMBOL = "SINGLE_SYMBOL"


@dataclass(frozen=True, slots=True)
class DailyCollectionPlan:
    provider: ProviderCode
    mode: DailyCollectionMode
    total_symbols: int
    group_size: int
    estimated_seconds_per_request: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", ProviderCode(self.provider))
        object.__setattr__(self, "mode", DailyCollectionMode(self.mode))
        if self.total_symbols < 1 or self.group_size < 1:
            raise ValueError("daily collection scope and group size must be positive")
        if self.estimated_seconds_per_request <= 0:
            raise ValueError("daily collection request estimate must be positive")

    @property
    def estimated_requests(self) -> int:
        if self.mode is DailyCollectionMode.SNAPSHOT:
            return 1
        return ceil(self.total_symbols / self.group_size)

    @property
    def estimated_seconds(self) -> int:
        return max(
            1,
            ceil(self.estimated_requests * self.estimated_seconds_per_request),
        )


@dataclass(frozen=True, slots=True)
class MarketDailyGroupRequest:
    trading_date: date
    symbols: tuple[str, ...]
    group_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.trading_date, date) or isinstance(
            self.trading_date, datetime
        ):
            raise ValueError("trading_date must be a date")
        if not self.symbols or self.group_index < 0:
            raise ValueError("daily collection group is invalid")
        for symbol in self.symbols:
            validate_symbol(symbol)
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("daily collection group contains duplicate symbols")


@dataclass(frozen=True, slots=True)
class ProviderSourceIdentity:
    adapter: ProviderAdapterCode
    upstream: ProviderCode
    interface: str
    capability: ProviderCapability
    algorithm_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter", ProviderAdapterCode(self.adapter))
        object.__setattr__(self, "upstream", ProviderCode(self.upstream))
        object.__setattr__(self, "capability", ProviderCapability(self.capability))
        if not self.interface.strip() or not self.algorithm_version.strip():
            raise ValueError("provider source identity is incomplete")

    @property
    def contract_version(self) -> str:
        return ":".join(
            (
                self.adapter.value,
                self.upstream.value,
                self.interface,
                self.capability.value,
                self.algorithm_version,
            )
        )


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    BONUS_SHARE = "BONUS_SHARE"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    COMPOSITE = "COMPOSITE"


def validate_symbol(symbol: str) -> str:
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", symbol)
    if not match:
        raise ValueError("invalid internal security symbol")
    code, market = match.groups()
    allowed = (
        market == "SH"
        and code.startswith("6")
        or market == "SZ"
        and code.startswith(("0", "3"))
        or market == "BJ"
        and code.startswith(("4", "8", "9"))
    )
    if not allowed:
        raise ValueError("symbol does not belong to market")
    return symbol


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone")


def _ohlc(open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> None:
    if any(value <= 0 for value in (open_, high, low, close)):
        raise ValueError("OHLC prices must be positive")
    if high < max(open_, close, low) or low > min(open_, close, high):
        raise ValueError("invalid OHLC range")


@dataclass(frozen=True, slots=True)
class SecurityMasterRecord:
    symbol: str
    name: str
    market: str
    security_type: str
    listed_on: date | None
    delisted_on: date | None
    listed: bool | None
    is_st: bool
    suspended: bool | None
    source: ProviderCode
    observed_at: datetime
    source_identity: ProviderSourceIdentity | None = None

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        _aware(self.observed_at)
        if self.market != self.symbol[-2:]:
            raise ValueError("market conflicts with symbol")


@dataclass(frozen=True, slots=True)
class RealtimeQuote:
    symbol: str
    price: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    previous_close: Decimal
    volume: int
    amount: Decimal
    quote_time: datetime
    received_at: datetime
    source: ProviderCode
    source_identity: ProviderSourceIdentity | None = None

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        _aware(self.quote_time)
        _aware(self.received_at)
        _ohlc(self.open, self.high, self.low, self.price)
        if self.previous_close < 0 or self.volume < 0 or self.amount < 0:
            raise ValueError("quantity and price fields cannot be negative")


@dataclass(frozen=True, slots=True)
class DailyBar:
    symbol: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal
    source: ProviderCode
    capability: ProviderCapability
    collected_at: datetime | None = None
    source_identity: ProviderSourceIdentity | None = None

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        _ohlc(self.open, self.high, self.low, self.close)
        if self.volume < 0 or self.amount < 0:
            raise ValueError("quantities cannot be negative")
        if self.capability not in {
            ProviderCapability.DAILY_BAR_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_QFQ,
        }:
            raise ValueError("invalid daily bar capability")
        if self.collected_at is not None:
            _aware(self.collected_at)
        if self.source_identity is not None:
            if self.source_identity.upstream is not self.source:
                raise ValueError("daily bar source identity conflicts with source")
            if self.source_identity.capability is not self.capability:
                raise ValueError("daily bar source identity conflicts with capability")


@dataclass(frozen=True, slots=True)
class DailyBarRequest:
    symbol: str
    start: date
    end: date
    capability: ProviderCapability

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if self.capability not in {
            ProviderCapability.DAILY_BAR_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_QFQ,
        }:
            raise ValueError("invalid daily bar capability")


@dataclass(frozen=True, slots=True)
class CorporateActionRequest:
    symbol: str
    start: date
    end: date

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        if self.start > self.end:
            raise ValueError("start must not be after end")


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    symbol: str
    source_event_id: str
    event_type: CorporateActionType
    event_date: date
    effective_date: date
    published_at: datetime
    observed_at: datetime
    adjustment_factor: Decimal
    source_reference: str
    raw_payload_hash: str
    source: ProviderCode

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        if not self.source_event_id.strip() or not self.source_reference.strip():
            raise ValueError("corporate action source identity is required")
        object.__setattr__(self, "event_type", CorporateActionType(self.event_type))
        _aware(self.published_at)
        _aware(self.observed_at)
        if self.published_at > self.observed_at:
            raise ValueError("corporate action cannot be observed before publication")
        if self.event_date > self.effective_date:
            raise ValueError("corporate action event date cannot follow effective date")
        if not self.adjustment_factor.is_finite() or self.adjustment_factor <= 0:
            raise ValueError("corporate action factor must be finite and positive")
        if re.fullmatch(r"[0-9a-f]{64}", self.raw_payload_hash) is None:
            raise ValueError("corporate action payload hash must be sha256")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    provider: ProviderCode
    capability: ProviderCapability
    healthy: bool
    checked_at: datetime
    latency_ms: int
    error_code: str | None = None

    def __post_init__(self) -> None:
        _aware(self.checked_at)
        if self.latency_ms < 0:
            raise ValueError("latency cannot be negative")


@dataclass(frozen=True, slots=True)
class ProviderItemFailure:
    symbol: str
    code: str
    message: str
    provider: ProviderCode

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)


@dataclass(frozen=True, slots=True)
class ProviderMissingRange:
    symbol: str
    start: date
    end: date

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        if self.start > self.end:
            raise ValueError("missing range start must not be after end")


@dataclass(frozen=True, slots=True)
class ProviderBatchResult[T]:
    items: tuple[T, ...] = ()
    failures: tuple[ProviderItemFailure, ...] = ()
    batch_error_code: str | None = None
    missing_ranges: tuple[ProviderMissingRange, ...] = ()


class MarketDataProvider(Protocol):
    @property
    def code(self) -> ProviderCode: ...

    @property
    def capabilities(self) -> frozenset[ProviderCapability]: ...

    def source_identity(
        self, capability: ProviderCapability
    ) -> ProviderSourceIdentity: ...

    async def security_master(
        self, deadline: datetime
    ) -> tuple[SecurityMasterRecord, ...]: ...

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]: ...

    async def daily_bars(
        self, request: DailyBarRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]: ...

    def daily_collection_plan(self, total_symbols: int) -> DailyCollectionPlan: ...

    async def market_daily_bars(
        self, request: MarketDailyGroupRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]: ...

    async def corporate_actions(
        self, request: CorporateActionRequest, deadline: datetime
    ) -> ProviderBatchResult[CorporateActionRecord]: ...

    async def probe(
        self, capability: ProviderCapability, deadline: datetime
    ) -> ProbeResult: ...
