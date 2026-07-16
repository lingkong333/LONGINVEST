import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import structlog

from long_invest.modules.providers.contracts import (
    ProviderBatchResult,
    ProviderCode,
    RealtimeQuote,
)
from long_invest.modules.quotes.contracts import (
    CreateQuoteCycle,
    QuoteCycleStatus,
    QuoteCycleSummary,
    QuoteSubmission,
)
from long_invest.modules.quotes.service import fallback_symbols

logger = structlog.get_logger(__name__)
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 5.0
TERMINAL_CYCLE_STATUSES = frozenset(
    {
        QuoteCycleStatus.READY,
        QuoteCycleStatus.PARTIAL,
        QuoteCycleStatus.FAILED,
        QuoteCycleStatus.MISSED,
        QuoteCycleStatus.CANCELED,
    }
)


class QuoteProviderPort(Protocol):
    async def realtime_quotes_from(
        self,
        provider_code: ProviderCode,
        symbols: tuple[str, ...],
        deadline: datetime,
    ) -> ProviderBatchResult[RealtimeQuote]: ...


class QuoteCyclePort(Protocol):
    async def create_and_start(
        self, command: CreateQuoteCycle, now: datetime
    ) -> QuoteCycleSummary: ...

    async def submit(
        self, cycle_id: UUID, submission: QuoteSubmission, now: datetime
    ) -> None: ...

    async def finalize(self, cycle_id: UUID, now: datetime) -> QuoteCycleSummary: ...

    async def cancel(
        self, cycle_id: UUID, now: datetime, reason: str
    ) -> QuoteCycleSummary: ...


class QuoteCollectionService:
    def __init__(
        self,
        provider: QuoteProviderPort,
        cycles: QuoteCyclePort,
        *,
        now: Callable[[], datetime] | None = None,
        cleanup_timeout_seconds: float = DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    ) -> None:
        if cleanup_timeout_seconds <= 0:
            raise ValueError("cleanup timeout must be positive")
        self._provider = provider
        self._cycles = cycles
        self._now = now or (lambda: datetime.now(UTC))
        self._cleanup_timeout_seconds = cleanup_timeout_seconds

    async def collect(self, command: CreateQuoteCycle) -> QuoteCycleSummary:
        started_at = self._now()
        start_task = asyncio.create_task(
            self._cycles.create_and_start(command, started_at)
        )
        try:
            cycle = await asyncio.shield(start_task)
        except asyncio.CancelledError as cancellation:
            cleanup_task = asyncio.create_task(
                self._cancel_after_start(start_task)
            )
            await self._wait_for_cleanup(
                cleanup_task,
                phase="create_and_start",
                cycle_id=None,
                related_tasks=(start_task,),
            )
            raise cancellation

        if cycle.status in TERMINAL_CYCLE_STATUSES:
            return cycle
        cycle_id = cycle.id
        deadline = cycle.deadline_at
        if deadline is None or self._now() >= deadline:
            return await self._cycles.finalize(cycle_id, self._now())

        try:
            return await self._collect_active(command, cycle_id, deadline)
        except asyncio.CancelledError as cancellation:
            cleanup_task = asyncio.create_task(self._cancel_cycle(cycle_id))
            await self._wait_for_cleanup(
                cleanup_task,
                phase="cancel_cycle",
                cycle_id=cycle_id,
            )
            raise cancellation

    async def _collect_active(
        self,
        command: CreateQuoteCycle,
        cycle_id: UUID,
        deadline: datetime,
    ) -> QuoteCycleSummary:
        primary = await self._fetch(
            ProviderCode.EASTMONEY, command.symbols, deadline
        )
        if self._now() >= deadline:
            return await self._cycles.finalize(cycle_id, self._now())
        fallback_scope = fallback_symbols(
            command.symbols, primary.items, now=self._now()
        )
        primary_by_symbol = {item.symbol: item for item in primary.items}
        for symbol in command.symbols:
            if symbol in fallback_scope:
                continue
            if self._now() >= deadline:
                return await self._cycles.finalize(cycle_id, self._now())
            await self._cycles.submit(
                cycle_id,
                QuoteSubmission(symbol=symbol, primary=primary_by_symbol[symbol]),
                self._now(),
            )
        fallback = (
            await self._fetch(ProviderCode.SINA, fallback_scope, deadline)
            if fallback_scope
            else ProviderBatchResult()
        )

        fallback_by_symbol = {item.symbol: item for item in fallback.items}
        failure_codes = {
            item.symbol: item.code for item in (*primary.failures, *fallback.failures)
        }
        for symbol in fallback_scope:
            if self._now() >= deadline:
                return await self._cycles.finalize(cycle_id, self._now())
            main = primary_by_symbol.get(symbol)
            backup = fallback_by_symbol.get(symbol)
            error_code = None
            if main is None and backup is None:
                error_code = (
                    failure_codes.get(symbol)
                    or fallback.batch_error_code
                    or primary.batch_error_code
                    or "QUOTE_ALL_PROVIDERS_FAILED"
                )
            await self._cycles.submit(
                cycle_id,
                QuoteSubmission(
                    symbol=symbol,
                    primary=main,
                    fallback=backup,
                    provider_error_code=error_code,
                ),
                self._now(),
            )
        return await self._cycles.finalize(cycle_id, self._now())

    async def _cancel_after_start(
        self, start_task: asyncio.Task[QuoteCycleSummary]
    ) -> None:
        try:
            cycle = await start_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_cleanup_failure("create_and_start", None, exc)
            return
        if cycle.status not in TERMINAL_CYCLE_STATUSES:
            await self._cancel_cycle(cycle.id)

    async def _cancel_cycle(self, cycle_id: UUID) -> None:
        try:
            await self._cycles.cancel(
                cycle_id, self._now(), "JOB_EXECUTION_CANCELED"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_cleanup_failure("cancel_cycle", cycle_id, exc)

    async def _wait_for_cleanup(
        self,
        cleanup_task: asyncio.Task[None],
        *,
        phase: str,
        cycle_id: UUID | None,
        related_tasks: tuple[asyncio.Task[object], ...] = (),
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._cleanup_timeout_seconds
        while not cleanup_task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=remaining)
            except TimeoutError:
                break
            except asyncio.CancelledError:
                continue
        if not cleanup_task.done():
            self._log_cleanup_timeout(phase, cycle_id)
            await self._cancel_and_reap((cleanup_task, *related_tasks))
            return
        if cleanup_task.cancelled():
            return
        cleanup_task.result()

    @staticmethod
    async def _cancel_and_reap(tasks: tuple[asyncio.Task[object], ...]) -> None:
        tracked = tuple(dict.fromkeys(tasks))
        for task in tracked:
            task.add_done_callback(_consume_task_result)
            if not task.done():
                task.cancel()
        for _ in range(3):
            if all(task.done() for task in tracked):
                break
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
        for task in tracked:
            if task.done():
                _consume_task_result(task)

    def _log_cleanup_timeout(self, phase: str, cycle_id: UUID | None) -> None:
        logger.error(
            "quote_cycle_cancellation_cleanup_timed_out",
            category="worker",
            phase=phase,
            cycle_id=str(cycle_id) if cycle_id is not None else None,
            timeout_seconds=self._cleanup_timeout_seconds,
        )

    @staticmethod
    def _log_cleanup_failure(
        phase: str, cycle_id: UUID | None, exc: BaseException
    ) -> None:
        logger.exception(
            "quote_cycle_cancellation_cleanup_failed",
            category="worker",
            phase=phase,
            cycle_id=str(cycle_id) if cycle_id is not None else None,
            error_type=type(exc).__name__,
        )

    async def _fetch(
        self,
        provider_code: ProviderCode,
        symbols: tuple[str, ...],
        deadline: datetime,
    ) -> ProviderBatchResult[RealtimeQuote]:
        try:
            return await self._provider.realtime_quotes_from(
                provider_code, symbols, deadline
            )
        except Exception as exc:
            return ProviderBatchResult(
                batch_error_code=str(getattr(exc, "code", "PROVIDER_FAILED"))
            )


def _consume_task_result(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    task.exception()
