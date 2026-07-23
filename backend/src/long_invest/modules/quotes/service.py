from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from long_invest.modules.market_data.contracts import OpenQualityIssue, QualitySeverity
from long_invest.modules.providers.contracts import RealtimeQuote
from long_invest.modules.quotes.contracts import (
    CreateQuoteCycle,
    QuoteCyclePage,
    QuoteCycleStatus,
    QuoteCycleSummary,
    QuoteItemStatus,
    QuoteItemView,
    QuoteOperationAction,
    QuoteSubmission,
)
from long_invest.modules.quotes.models import QuoteCycle, QuoteCycleItem
from long_invest.modules.quotes.quality import compare_quotes, validate_quote
from long_invest.platform.errors import AppError

TERMINAL_CYCLES = frozenset(
    {
        QuoteCycleStatus.READY,
        QuoteCycleStatus.PARTIAL,
        QuoteCycleStatus.FAILED,
        QuoteCycleStatus.MISSED,
        QuoteCycleStatus.CANCELED,
    }
)


def quote_operation_allowed_actions(
    *,
    manual_collection_in_progress: bool,
    diagnosis_in_progress: bool,
) -> tuple[QuoteOperationAction, ...]:
    actions: list[QuoteOperationAction] = []
    if not manual_collection_in_progress:
        actions.append(QuoteOperationAction.MANUAL_COLLECT)
    if not diagnosis_in_progress:
        actions.append(QuoteOperationAction.DIAGNOSE)
    return tuple(actions)


MISSED_CLAIM_WINDOW_SECONDS = 60


def fallback_symbols(
    expected_symbols: tuple[str, ...],
    primary_quotes: tuple[RealtimeQuote, ...],
    *,
    now: datetime,
) -> tuple[str, ...]:
    """Return the frozen symbols whose primary quote is absent or invalid."""
    primary_by_symbol = {quote.symbol: quote for quote in primary_quotes}
    return tuple(
        symbol
        for symbol in expected_symbols
        if (quote := primary_by_symbol.get(symbol)) is None
        or not validate_quote(quote, symbol=symbol, now=now).valid
    )


class QuoteRepositoryPort(Protocol):
    session: object

    async def claim_cycle(self, cycle: QuoteCycle) -> tuple[QuoteCycle, bool]: ...
    async def get_with_items(self, cycle_id: UUID) -> QuoteCycle | None: ...
    async def get_for_finalize(self, cycle_id: UUID) -> QuoteCycle | None: ...
    async def get_for_update(self, cycle_id: UUID) -> QuoteCycle | None: ...
    async def get_item_for_update(
        self, cycle_id: UUID, symbol: str
    ) -> QuoteCycleItem | None: ...
    async def list(self, **kwargs: object) -> list[QuoteCycle]: ...
    async def count(self, **kwargs: object) -> int: ...
    async def list_items(
        self, cycle_id: UUID, **kwargs: object
    ) -> list[QuoteCycleItem]: ...
    async def find_expired(self, now: datetime, limit: int) -> list[QuoteCycle]: ...
    async def flush(self) -> None: ...


class QuoteEventPort(Protocol):
    session: object

    async def created(self, cycle: QuoteCycle) -> None: ...
    async def conflict(self, cycle: QuoteCycle, item: QuoteCycleItem) -> None: ...
    async def finalized(
        self, cycle: QuoteCycle, valid_items: list[QuoteCycleItem]
    ) -> None: ...
    async def missing(
        self, cycle: QuoteCycle, abnormal_items: list[QuoteCycleItem]
    ) -> None: ...


class QualityIssuePort(Protocol):
    async def open(self, command: OpenQualityIssue) -> object: ...


class QuoteCycleService:
    def __init__(
        self,
        repository: QuoteRepositoryPort,
        *,
        events: QuoteEventPort,
        quality_issues: QualityIssuePort,
    ) -> None:
        if events.session is not repository.session:
            raise AppError(
                code="QUOTE_TRANSACTION_MISMATCH",
                message="行情事实与事件必须位于同一事务",
                status_code=500,
            )
        self._repository = repository
        self._events = events
        self._quality = quality_issues

    async def create(self, command: CreateQuoteCycle) -> QuoteCycleSummary:
        cycle = QuoteCycle(
            status=QuoteCycleStatus.PENDING,
            scheduled_at=command.scheduled_at,
            universe_snapshot_id=command.universe_snapshot_id,
            universe_snapshot_version=command.universe_snapshot_version,
            idempotency_scope=command.idempotency_scope,
            idempotency_key=command.idempotency_key,
            expected_count=len(command.symbols),
            timeout_seconds=command.timeout_seconds,
            schedule_occurrence_id=command.schedule_occurrence_id,
            subscription_snapshot_version=command.subscription_snapshot_version,
        )
        cycle.items = [
            QuoteCycleItem(
                symbol=symbol,
                status=QuoteItemStatus.MISSING,
                error_code=None,
                expected_subscription_version=command.subscription_snapshot_version,
            )
            for symbol in command.symbols
        ]
        claimed, created = await self._repository.claim_cycle(cycle)
        if created:
            await self._events.created(claimed)
        elif not _same_create_request(claimed, command):
            if command.schedule_occurrence_id is not None:
                raise AppError(
                    code="QUOTE_CYCLE_OCCURRENCE_CONFLICT",
                    message="该计划发生号已用于不同的行情批次",
                    status_code=409,
                )
            raise AppError(
                code="IDEMPOTENCY_KEY_CONFLICT",
                message="该幂等键已用于不同的行情批次",
                status_code=409,
            )
        return _summary(claimed)

    async def start(self, cycle_id: UUID, now: datetime) -> QuoteCycleSummary:
        cycle = await self._repository.get_for_update(cycle_id)
        try:
            if cycle is None:
                raise _not_found()
            if QuoteCycleStatus(cycle.status) is QuoteCycleStatus.PENDING:
                cycle.status = QuoteCycleStatus.FETCHING
                cycle.started_at = now
                cycle.deadline_at = now + timedelta(seconds=cycle.timeout_seconds)
                await self._repository.flush()
            elif QuoteCycleStatus(cycle.status) not in TERMINAL_CYCLES | {
                QuoteCycleStatus.FETCHING
            }:
                raise _state_error()
            return _summary(cycle)
        finally:
            await _release_test_lock(self._repository)

    async def submit(
        self, cycle_id: UUID, submission: QuoteSubmission, now: datetime
    ) -> None:
        cycle = await self._repository.get_for_update(cycle_id)
        try:
            if cycle is None:
                raise _not_found()
            if QuoteCycleStatus(cycle.status) in TERMINAL_CYCLES:
                return
            if QuoteCycleStatus(cycle.status) is not QuoteCycleStatus.FETCHING:
                raise _state_error()
            if cycle.deadline_at is not None and now >= cycle.deadline_at:
                raise AppError(
                    code="QUOTE_CYCLE_DEADLINE_EXCEEDED",
                    message="行情批次已超过截止时间",
                    status_code=409,
                )
            item = await self._repository.get_item_for_update(
                cycle_id, submission.symbol
            )
            if item is None:
                raise AppError(
                    code="QUOTE_ITEM_NOT_IN_SCOPE",
                    message="股票不在冻结范围内",
                    status_code=422,
                )
            if item.error_code is not None or item.status != QuoteItemStatus.MISSING:
                return
            if submission.not_expected_to_trade:
                _set_terminal(
                    item,
                    QuoteItemStatus.NOT_EXPECTED_TO_TRADE,
                    "NOT_EXPECTED_TO_TRADE",
                )
            else:
                await self._apply_submission(cycle, item, submission, now)
            await self._repository.flush()
        finally:
            await _release_test_lock(self._repository)

    async def _apply_submission(
        self,
        cycle: QuoteCycle,
        item: QuoteCycleItem,
        submission: QuoteSubmission,
        now: datetime,
    ) -> None:
        valid = []
        invalid_codes = []
        for candidate in (submission.primary, submission.fallback):
            if candidate is None:
                continue
            result = validate_quote(candidate, symbol=item.symbol, now=now)
            if result.valid:
                valid.append(candidate)
            elif result.error_code:
                invalid_codes.append(result.error_code)
        if len(valid) == 2 and compare_quotes(valid[0], valid[1]).conflict:
            item.status = QuoteItemStatus.CONFLICT
            item.error_code = "QUOTE_CONFLICT"
            item.conflict_evidence = {
                "sources": {
                    str(valid[0].source): _quote_evidence(valid[0]),
                    str(valid[1].source): _quote_evidence(valid[1]),
                }
            }
            item.eligible_for_evaluation = False
            await self._quality.open(
                OpenQualityIssue(
                    issue_type="QUOTE_CONFLICT",
                    subject_type="quote_cycle_item",
                    subject_id=str(item.id),
                    symbol=item.symbol,
                    severity=QualitySeverity.WARNING,
                    evidence=item.conflict_evidence,
                    dedupe_key=f"quote:{item.id}:conflict",
                    requires_review=True,
                )
            )
            await self._events.conflict(cycle, item)
            return
        if valid:
            _copy_quote(item, valid[0])
            return
        code = submission.provider_error_code or (
            invalid_codes[0] if invalid_codes else "QUOTE_ALL_PROVIDERS_FAILED"
        )
        status = (
            QuoteItemStatus.STALE
            if code == "QUOTE_STALE"
            else (
                QuoteItemStatus.PROVIDER_FAILED
                if submission.provider_error_code
                else QuoteItemStatus.INVALID
            )
        )
        _set_terminal(item, status, code)

    async def finalize(self, cycle_id: UUID, now: datetime) -> QuoteCycleSummary:
        cycle = await self._repository.get_for_update(cycle_id)
        try:
            if cycle is None:
                raise _not_found()
            if QuoteCycleStatus(cycle.status) in TERMINAL_CYCLES:
                return _summary(cycle)
            if QuoteCycleStatus(cycle.status) not in {
                QuoteCycleStatus.FETCHING,
                QuoteCycleStatus.FINALIZING,
            }:
                raise _state_error()
            pending = [
                item
                for item in cycle.items
                if item.status == QuoteItemStatus.MISSING and item.error_code is None
            ]
            if pending and (cycle.deadline_at is None or now < cycle.deadline_at):
                raise AppError(
                    code="QUOTE_CYCLE_STATE_CONFLICT",
                    message="批次仍在等待报价",
                    status_code=409,
                )
            cycle.status = QuoteCycleStatus.FINALIZING
            for item in pending:
                _set_terminal(
                    item, QuoteItemStatus.TIMEOUT, "QUOTE_CYCLE_DEADLINE_EXCEEDED"
                )
            valid_items = [
                item for item in cycle.items if item.status == QuoteItemStatus.VALID
            ]
            neutral = [
                item
                for item in cycle.items
                if item.status == QuoteItemStatus.NOT_EXPECTED_TO_TRADE
            ]
            abnormal = [
                item
                for item in cycle.items
                if item not in valid_items and item not in neutral
            ]
            cycle.valid_count = len(valid_items)
            cycle.missing_count = sum(
                item.status in {QuoteItemStatus.MISSING, QuoteItemStatus.TIMEOUT}
                for item in abnormal
            )
            cycle.conflict_count = sum(
                item.status == QuoteItemStatus.CONFLICT for item in abnormal
            )
            cycle.failed_count = (
                len(abnormal) - cycle.missing_count - cycle.conflict_count
            )
            cycle.status = (
                QuoteCycleStatus.READY
                if not abnormal
                else (
                    QuoteCycleStatus.PARTIAL if valid_items else QuoteCycleStatus.FAILED
                )
            )
            cycle.finalized_at = now
            await self._repository.flush()
            await self._events.finalized(cycle, valid_items)
            if abnormal:
                await self._events.missing(cycle, abnormal)
            return _summary(cycle)
        finally:
            await _release_test_lock(self._repository)

    async def mark_missed(self, cycle_id: UUID, now: datetime) -> QuoteCycleSummary:
        cycle = await self._repository.get_for_update(cycle_id)
        try:
            if cycle is None:
                raise _not_found()
            if QuoteCycleStatus(cycle.status) in TERMINAL_CYCLES:
                return _summary(cycle)
            if (
                QuoteCycleStatus(cycle.status) is not QuoteCycleStatus.PENDING
                or cycle.started_at is not None
                or now
                <= cycle.scheduled_at + timedelta(seconds=MISSED_CLAIM_WINDOW_SECONDS)
            ):
                raise _state_error()
            cycle.status = QuoteCycleStatus.MISSED
            cycle.finalized_at = now
            await self._repository.flush()
            return _summary(cycle)
        finally:
            await _release_test_lock(self._repository)

    async def cancel(
        self, cycle_id: UUID, now: datetime, reason: str
    ) -> QuoteCycleSummary:
        cycle = await self._repository.get_for_update(cycle_id)
        try:
            if cycle is None:
                raise _not_found()
            if QuoteCycleStatus(cycle.status) in TERMINAL_CYCLES:
                return _summary(cycle)
            if QuoteCycleStatus(cycle.status) not in {
                QuoteCycleStatus.PENDING,
                QuoteCycleStatus.FETCHING,
            }:
                raise _state_error()
            cycle.status = QuoteCycleStatus.CANCELED
            cycle.finalized_at = now
            cycle.cancel_reason = reason
            for item in cycle.items:
                item.eligible_for_evaluation = False
            await self._repository.flush()
            return _summary(cycle)
        finally:
            await _release_test_lock(self._repository)

    async def recover_expired(
        self, now: datetime, limit: int = 100
    ) -> tuple[UUID, ...]:
        recovered = []
        for cycle in await self._repository.find_expired(now, limit):
            await self.finalize(cycle.id, now)
            recovered.append(cycle.id)
        return tuple(recovered)

    async def list(
        self,
        *,
        status: QuoteCycleStatus | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> QuoteCyclePage:
        cycles = await self._repository.list(
            status=status, page=page, page_size=page_size
        )
        return QuoteCyclePage(
            tuple(_summary(c) for c in cycles),
            await self._repository.count(status=status),
            page,
            page_size,
        )

    async def list_items(
        self, cycle_id: UUID, *, page: int = 1, page_size: int = 200
    ) -> tuple[QuoteItemView, ...]:
        if await self._repository.get_with_items(cycle_id) is None:
            raise _not_found()
        return tuple(
            _item_view(item)
            for item in await self._repository.list_items(
                cycle_id, page=page, page_size=page_size
            )
        )

    async def _required(self, cycle_id: UUID) -> QuoteCycle:
        cycle = await self._repository.get_with_items(cycle_id)
        if cycle is None:
            raise _not_found()
        return cycle


def _copy_quote(item: QuoteCycleItem, quote: RealtimeQuote) -> None:
    for name in (
        "price",
        "open",
        "high",
        "low",
        "previous_close",
        "volume",
        "amount",
        "quote_time",
        "received_at",
    ):
        setattr(item, name, getattr(quote, name))
    item.provider = quote.source
    item.status = QuoteItemStatus.VALID
    item.error_code = None
    item.eligible_for_evaluation = True


def _set_terminal(item: QuoteCycleItem, status: QuoteItemStatus, code: str) -> None:
    item.status = status
    item.error_code = code
    item.eligible_for_evaluation = False


def _quote_evidence(quote: RealtimeQuote) -> dict[str, object]:
    return {
        "symbol": quote.symbol,
        "price": str(quote.price),
        "open": str(quote.open),
        "high": str(quote.high),
        "low": str(quote.low),
        "previous_close": str(quote.previous_close),
        "volume": quote.volume,
        "amount": str(quote.amount),
        "quote_time": quote.quote_time.isoformat(),
        "received_at": quote.received_at.isoformat(),
        "source": str(quote.source),
    }


def _summary(cycle: QuoteCycle) -> QuoteCycleSummary:
    valid = [
        item
        for item in cycle.items
        if item.status == QuoteItemStatus.VALID and item.eligible_for_evaluation
    ]
    return QuoteCycleSummary(
        id=cycle.id,
        status=QuoteCycleStatus(cycle.status),
        expected_count=cycle.expected_count,
        valid_count=cycle.valid_count,
        missing_count=cycle.missing_count,
        conflict_count=cycle.conflict_count,
        failed_count=cycle.failed_count,
        eligible_item_ids=tuple(i.id for i in valid),
        eligible_symbols=tuple(i.symbol for i in valid),
        scheduled_at=cycle.scheduled_at,
        started_at=cycle.started_at,
        deadline_at=cycle.deadline_at,
        finalized_at=cycle.finalized_at,
        schedule_occurrence_id=cycle.schedule_occurrence_id,
        subscription_snapshot_version=cycle.subscription_snapshot_version,
    )


def _item_view(item: QuoteCycleItem) -> QuoteItemView:
    return QuoteItemView(
        id=item.id,
        cycle_id=item.cycle_id,
        symbol=item.symbol,
        status=QuoteItemStatus(item.status),
        price=item.price,
        open=item.open,
        high=item.high,
        low=item.low,
        previous_close=item.previous_close,
        volume=item.volume,
        amount=item.amount,
        quote_time=item.quote_time,
        received_at=item.received_at,
        provider=item.provider,
        error_code=item.error_code,
        conflict_evidence=item.conflict_evidence,
        eligible_for_evaluation=item.eligible_for_evaluation,
        expected_subscription_version=item.expected_subscription_version,
    )


def _not_found() -> AppError:
    return AppError(
        code="QUOTE_CYCLE_NOT_FOUND", message="行情批次不存在", status_code=404
    )


def _state_error() -> AppError:
    return AppError(
        code="QUOTE_CYCLE_STATE_CONFLICT",
        message="行情批次状态不允许该操作",
        status_code=409,
    )


async def _release_test_lock(repository: QuoteRepositoryPort) -> None:
    release = getattr(repository, "release_finalize", None)
    if release is not None:
        await release()


def _same_create_request(cycle: QuoteCycle, command: CreateQuoteCycle) -> bool:
    return (
        tuple(sorted(item.symbol for item in cycle.items))
        == tuple(sorted(command.symbols))
        and cycle.scheduled_at == command.scheduled_at
        and cycle.timeout_seconds == command.timeout_seconds
        and cycle.universe_snapshot_id == command.universe_snapshot_id
        and cycle.universe_snapshot_version == command.universe_snapshot_version
        and cycle.schedule_occurrence_id == command.schedule_occurrence_id
        and cycle.subscription_snapshot_version == command.subscription_snapshot_version
    )
