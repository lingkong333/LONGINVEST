from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Generic, Protocol, TypeVar


class ProviderCapability(StrEnum):
    SECURITY_MASTER = "SECURITY_MASTER"
    REALTIME_QUOTE_BATCH = "REALTIME_QUOTE_BATCH"
    DAILY_BAR_UNADJUSTED = "DAILY_BAR_UNADJUSTED"
    HISTORICAL_DAILY_UNADJUSTED = "HISTORICAL_DAILY_UNADJUSTED"
    HISTORICAL_DAILY_QFQ = "HISTORICAL_DAILY_QFQ"


class ProviderCode(StrEnum):
    EASTMONEY = "EASTMONEY"
    SINA = "SINA"


def validate_symbol(symbol: str) -> str:
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", symbol)
    if not match:
        raise ValueError("invalid internal security symbol")
    code, market = match.groups()
    allowed = (
        market == "SH" and code.startswith("6")
        or market == "SZ" and code.startswith(("0", "3"))
        or market == "BJ" and code.startswith(("4", "8", "9"))
    )
    if not allowed:
        raise ValueError("symbol does not belong to market")
    return symbol


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone")


def _ohlc(open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> None:
    if any(value < 0 for value in (open_, high, low, close)):
        raise ValueError("prices cannot be negative")
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
    listed: bool
    is_st: bool
    suspended: bool
    source: ProviderCode
    observed_at: datetime

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


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProviderBatchResult(Generic[T]):
    items: tuple[T, ...] = ()
    failures: tuple[ProviderItemFailure, ...] = ()
    batch_error_code: str | None = None


class MarketDataProvider(Protocol):
    @property
    def code(self) -> ProviderCode: ...

    @property
    def capabilities(self) -> frozenset[ProviderCapability]: ...

    async def security_master(self, deadline: datetime) -> tuple[SecurityMasterRecord, ...]: ...

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]: ...

    async def daily_bars(
        self, request: DailyBarRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]: ...

    async def probe(
        self, capability: ProviderCapability, deadline: datetime
    ) -> ProbeResult: ...
