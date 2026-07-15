import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Market(StrEnum):
    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"
    HK = "HK"
    US = "US"


class SecurityType(StrEnum):
    A_SHARE = "A_SHARE"
    ETF = "ETF"
    CONVERTIBLE_BOND = "CONVERTIBLE_BOND"
    B_SHARE = "B_SHARE"
    FUND = "FUND"
    INDEX = "INDEX"
    HK_STOCK = "HK_STOCK"
    US_STOCK = "US_STOCK"


class ListingStatus(StrEnum):
    LISTED = "LISTED"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    DATA_MISSING = "DATA_MISSING"


_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")


def validate_symbol(symbol: str) -> str:
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("统一代码必须为 600000.SH/000001.SZ/430047.BJ 形式")
    return symbol


@dataclass(frozen=True, slots=True)
class SecurityMasterItem:
    symbol: str
    exchange_code: str
    name: str
    market: Market
    security_type: SecurityType
    listing_status: ListingStatus
    listed_on: date | None
    delisted_on: date | None
    is_st: bool
    is_suspended: bool
    provider_codes: Mapping[str, str]

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)


@dataclass(frozen=True, slots=True)
class SecurityMasterSnapshot:
    source: str
    source_version: str
    idempotency_key: str
    items: tuple[SecurityMasterItem, ...]


@dataclass(frozen=True, slots=True)
class SecurityEligibility:
    eligible: bool
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class UniverseQuery:
    markets: tuple[Market, ...] = (Market.SH, Market.SZ, Market.BJ)
    security_types: tuple[SecurityType, ...] = (SecurityType.A_SHARE,)
    listing_statuses: tuple[ListingStatus, ...] = (
        ListingStatus.LISTED,
        ListingStatus.SUSPENDED,
    )
    include_st: bool = True
    include_suspended: bool = True
    filters: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    master_version: int
    total_count: int
    created_count: int
    updated_count: int
    unchanged_count: int
    revision_count: int
    replayed: bool = False


def assess_monitoring_eligibility(item: SecurityMasterItem) -> SecurityEligibility:
    if item.security_type is not SecurityType.A_SHARE or item.market not in {
        Market.SH,
        Market.SZ,
        Market.BJ,
    }:
        return SecurityEligibility(
            eligible=False,
            code="SECURITY_TYPE_UNSUPPORTED",
            message="该证券类型不支持正式监控",
        )
    if item.listing_status is ListingStatus.DELISTED:
        return SecurityEligibility(
            eligible=False,
            code="SECURITY_DELISTED",
            message="退市股票不能新增正式监控",
        )
    if item.listing_status is ListingStatus.DATA_MISSING:
        return SecurityEligibility(
            eligible=False,
            code="SECURITY_DATA_MISSING",
            message="证券主数据暂缺，不能新增正式监控",
        )
    return SecurityEligibility(
        eligible=True,
        code="ELIGIBLE",
        message="该股票可建立正式监控",
    )
