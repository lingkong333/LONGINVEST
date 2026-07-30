from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.providers.contracts import validate_symbol


class HistoryBackfillScope(StrEnum):
    SINGLE = "SINGLE"
    SELECTED = "SELECTED"
    WATCHLIST = "WATCHLIST"
    ALL = "ALL"


class HistoryBackfillAction(StrEnum):
    CREATE = "CREATE"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"
    RETRY_FAILED = "RETRY_FAILED"


@dataclass(frozen=True, slots=True)
class CreateHistoryBackfill:
    scope: HistoryBackfillScope
    start_date: date | None
    end_date: date | None
    concurrency: int
    symbols: tuple[str, ...] = ()
    watchlist_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", HistoryBackfillScope(self.scope))
        normalized = tuple(sorted(set(self.symbols)))
        for symbol in normalized:
            validate_symbol(symbol)
        object.__setattr__(self, "symbols", normalized)
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("开始日期和结束日期必须同时填写或同时留空")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("开始日期不能晚于结束日期")
        if self.concurrency < 1:
            raise ValueError("并发数必须是正整数")
        if self.scope is HistoryBackfillScope.SINGLE and len(normalized) != 1:
            raise ValueError("单股回填必须且只能选择一只股票")
        if self.scope is HistoryBackfillScope.SELECTED and not normalized:
            raise ValueError("选择股票回填时股票范围不能为空")
        if (
            self.scope in {HistoryBackfillScope.WATCHLIST, HistoryBackfillScope.ALL}
            and normalized
        ):
            raise ValueError("该回填范围不能同时指定股票代码")
        if (self.scope is HistoryBackfillScope.WATCHLIST) != (
            self.watchlist_id is not None
        ):
            raise ValueError("监控列表回填必须且只能指定一个监控列表")


@dataclass(frozen=True, slots=True)
class FrozenHistorySecurity:
    security_id: UUID
    symbol: str


@dataclass(frozen=True, slots=True)
class FrozenHistoryScope:
    snapshot_id: UUID
    master_version: int
    items: tuple[FrozenHistorySecurity, ...]

    def __post_init__(self) -> None:
        if self.master_version <= 0:
            raise ValueError("主数据版本必须大于 0")
        if not self.items:
            raise ValueError("冻结股票范围不能为空")
        symbols = tuple(item.symbol for item in self.items)
        if len(symbols) != len(set(symbols)):
            raise ValueError("冻结股票范围不能包含重复股票")


@dataclass(frozen=True, slots=True)
class HistoryBackfillAuditContext:
    request_id: str
    idempotency_key: str
    actor_user_id: str
    session_id: str | None
    trusted_ip: str | None
    reason: str

    def __post_init__(self) -> None:
        required = (
            self.request_id,
            self.idempotency_key,
            self.actor_user_id,
            self.reason,
        )
        if any(not value.strip() for value in required):
            raise ValueError("历史回填审计上下文不完整")


class HistoryScopeSnapshotPort(Protocol):
    async def freeze(
        self,
        session: AsyncSession,
        command: CreateHistoryBackfill,
        *,
        owner_user_id: UUID,
    ) -> FrozenHistoryScope: ...


class HistoryDateRangePort(Protocol):
    async def complete_range(self) -> tuple[date, date]: ...


@dataclass(frozen=True, slots=True)
class HistoryBackfillWorkItem:
    security_id: UUID
    symbol: str
    attempt_count: int = 0


@dataclass(frozen=True, slots=True)
class HistoryBarInput:
    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal
    source: str
    source_identity: dict[str, str] | None = None
    collected_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HistoryBarsBundle:
    unadjusted: tuple[HistoryBarInput, ...]
    qfq: tuple[HistoryBarInput, ...]
    provider_contract_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "unadjusted", tuple(self.unadjusted))
        object.__setattr__(self, "qfq", tuple(self.qfq))
        if not self.provider_contract_version.strip():
            raise ValueError("history provider contract version is required")


@dataclass(frozen=True, slots=True)
class HistoryBarStoreResult:
    inserted: int
    unchanged: int
    revised: int
    qfq_dataset_id: UUID
    qfq_version: int
    qfq_rows: int
    qfq_unchanged: bool
    qfq_actual_start: date
    qfq_actual_end: date
    qfq_truncated_rows: int
    review_required: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.inserted,
                self.unchanged,
                self.revised,
                self.review_required,
                self.qfq_version,
                self.qfq_rows,
                self.qfq_truncated_rows,
            )
        ):
            raise ValueError("历史日线写入数量不能为负数")


class HistoryBackfillItemError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class HistoryBarsProviderPort(Protocol):
    async def fetch(
        self,
        item: HistoryBackfillWorkItem,
        *,
        start_date: date,
        end_date: date,
        deadline: datetime,
        concurrency: int,
    ) -> HistoryBarsBundle: ...


class HistoryBarStorePort(Protocol):
    async def store(
        self,
        item: HistoryBackfillWorkItem,
        bars: HistoryBarsBundle,
        *,
        idempotency_key: str,
        reason: str,
    ) -> HistoryBarStoreResult: ...


class HistoryDiskGuardPort(Protocol):
    async def is_backfill_safe(self) -> bool: ...
