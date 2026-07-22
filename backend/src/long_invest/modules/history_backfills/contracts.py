from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.providers.contracts import validate_symbol
from long_invest.platform.jobs.contracts import JobItemStatus


class HistoryBackfillScope(StrEnum):
    SINGLE = "SINGLE"
    SELECTED = "SELECTED"
    WATCHLIST = "WATCHLIST"
    ALL = "ALL"


class HistoryBackfillControl(StrEnum):
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"


@dataclass(frozen=True, slots=True)
class CreateHistoryBackfill:
    scope: HistoryBackfillScope
    start_date: date
    end_date: date
    concurrency: int
    symbols: tuple[str, ...] = ()
    watchlist_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", HistoryBackfillScope(self.scope))
        normalized = tuple(sorted(set(self.symbols)))
        for symbol in normalized:
            validate_symbol(symbol)
        object.__setattr__(self, "symbols", normalized)
        if self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        if not 1 <= self.concurrency <= 8:
            raise ValueError("并发数必须在 1 到 8 之间")
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


@dataclass(frozen=True, slots=True)
class HistoryBackfillExecutionResult:
    total: int
    succeeded: int
    failed: int
    canceled: int = 0
    pending: int = 0
    control: HistoryBackfillControl = HistoryBackfillControl.RUNNING

    def __post_init__(self) -> None:
        object.__setattr__(self, "control", HistoryBackfillControl(self.control))
        counts = (
            self.total,
            self.succeeded,
            self.failed,
            self.canceled,
            self.pending,
        )
        if any(value < 0 for value in counts):
            raise ValueError("历史回填结果数量不能为负数")
        if self.succeeded + self.failed + self.canceled + self.pending > self.total:
            raise ValueError("历史回填结果数量超过冻结范围")


@dataclass(frozen=True, slots=True)
class HistoryBackfillWorkItem:
    security_id: UUID
    symbol: str
    attempt_count: int = 0


@dataclass(frozen=True, slots=True)
class HistoryJobFence:
    job_id: UUID
    fence_token: UUID


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


@dataclass(frozen=True, slots=True)
class HistoryBarStoreResult:
    inserted: int
    unchanged: int
    revised: int
    review_required: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.inserted,
                self.unchanged,
                self.revised,
                self.review_required,
            )
        ):
            raise ValueError("历史日线写入数量不能为负数")


@dataclass(frozen=True, slots=True)
class HistoryJobItemSummary:
    total: int
    pending: int
    active: int
    succeeded: int
    failed: int
    canceled: int

    def __post_init__(self) -> None:
        counts = (
            self.total,
            self.pending,
            self.active,
            self.succeeded,
            self.failed,
            self.canceled,
        )
        if any(value < 0 for value in counts):
            raise ValueError("历史回填任务项数量不能为负数")
        if sum(counts[1:]) != self.total:
            raise ValueError("历史回填任务项汇总不一致")


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
    ) -> tuple[HistoryBarInput, ...]: ...


class HistoryBarStorePort(Protocol):
    async def store(
        self,
        item: HistoryBackfillWorkItem,
        bars: tuple[HistoryBarInput, ...],
        *,
        idempotency_key: str,
        reason: str,
    ) -> HistoryBarStoreResult: ...


class HistoryJobItemsPort(Protocol):
    async def recover_incomplete(self, fence: HistoryJobFence) -> None: ...

    async def control(self, fence: HistoryJobFence) -> HistoryBackfillControl: ...

    async def claim_pending(
        self, fence: HistoryJobFence, *, limit: int
    ) -> tuple[HistoryBackfillWorkItem, ...]: ...

    async def mark_stage(
        self, fence: HistoryJobFence, symbol: str, status: JobItemStatus
    ) -> None: ...

    async def release_pending(self, fence: HistoryJobFence, symbol: str) -> None: ...

    async def finish(
        self,
        fence: HistoryJobFence,
        symbol: str,
        *,
        status: JobItemStatus,
        result_ref: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None: ...

    async def summary(self, fence: HistoryJobFence) -> HistoryJobItemSummary: ...

    async def report_progress(
        self, fence: HistoryJobFence, summary: HistoryJobItemSummary
    ) -> None: ...

    async def request_pause(self, fence: HistoryJobFence, *, reason: str) -> None: ...

    async def cancel_pending(self, fence: HistoryJobFence) -> None: ...


class HistoryDiskGuardPort(Protocol):
    async def is_backfill_safe(self) -> bool: ...
