from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from long_invest.modules.history_backfills.contracts import (
    HistoryBackfillControl,
    HistoryBackfillExecutionResult,
    HistoryBackfillItemError,
    HistoryBackfillWorkItem,
    HistoryBarsProviderPort,
    HistoryBarStorePort,
    HistoryDiskGuardPort,
    HistoryJobFence,
    HistoryJobItemsPort,
)
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobItemStatus,
    JobResult,
)


class HistoryBackfillExecutor:
    def __init__(
        self,
        *,
        provider: HistoryBarsProviderPort,
        store: HistoryBarStorePort,
        items: HistoryJobItemsPort,
        disk_guard: HistoryDiskGuardPort,
        item_timeout_seconds: float = 600,
    ) -> None:
        if item_timeout_seconds <= 0:
            raise ValueError("item timeout must be positive")
        self._provider = provider
        self._store = store
        self._items = items
        self._disk_guard = disk_guard
        self._item_timeout_seconds = item_timeout_seconds

    async def execute(
        self,
        context: JobExecutionContext,
        *,
        start_date: date,
        end_date: date,
        concurrency: int,
        reason: str,
    ) -> HistoryBackfillExecutionResult:
        if start_date > end_date or not 1 <= concurrency <= 8 or not reason.strip():
            raise ValueError("invalid history execution configuration")
        fence = HistoryJobFence(context.job_id, context.fence_token)
        await self._items.recover_incomplete(fence)
        while True:
            control = await self._items.control(fence)
            if control is HistoryBackfillControl.CANCEL_REQUESTED:
                await self._items.cancel_pending(fence)
                break
            if control is HistoryBackfillControl.PAUSE_REQUESTED:
                break
            if not await self._disk_guard.is_backfill_safe():
                await self._items.request_pause(
                    fence, reason="HISTORY_DISK_CAPACITY_LOW"
                )
                break
            claimed = await self._items.claim_pending(fence, limit=concurrency)
            if not claimed:
                break
            await asyncio.gather(
                *(
                    self._process_item(
                        context,
                        fence,
                        item,
                        start_date=start_date,
                        end_date=end_date,
                        reason=reason,
                    )
                    for item in claimed
                )
            )
            await self._report_progress(fence)
        summary = await self._items.summary(fence)
        await self._items.report_progress(fence, summary)
        control = await self._items.control(fence)
        return HistoryBackfillExecutionResult(
            total=summary.total,
            succeeded=summary.succeeded,
            failed=summary.failed,
            canceled=summary.canceled,
            pending=summary.pending + summary.active,
            control=control,
        )

    async def _process_item(
        self,
        context: JobExecutionContext,
        fence: HistoryJobFence,
        item: HistoryBackfillWorkItem,
        *,
        start_date: date,
        end_date: date,
        reason: str,
    ) -> None:
        try:
            if await self._stop_at_safe_point(fence, item):
                return
            deadline = datetime.now(UTC) + timedelta(seconds=self._item_timeout_seconds)
            async with asyncio.timeout(self._item_timeout_seconds):
                bars = await self._provider.fetch(
                    item,
                    start_date=start_date,
                    end_date=end_date,
                    deadline=deadline,
                )
            await self._items.mark_stage(fence, item.symbol, JobItemStatus.VALIDATING)
            bars = _validate_bars(
                bars,
                symbol=item.symbol,
                start_date=start_date,
                end_date=end_date,
            )
            if not await self._disk_guard.is_backfill_safe():
                await self._items.request_pause(
                    fence, reason="HISTORY_DISK_CAPACITY_LOW"
                )
            if await self._stop_at_safe_point(fence, item):
                return
            await self._items.mark_stage(fence, item.symbol, JobItemStatus.SAVING)
            stored = await self._store.store(
                item,
                bars,
                idempotency_key=_store_key(
                    context.job_id, item.symbol, start_date, end_date
                ),
                reason=reason,
            )
            await self._items.finish(
                fence,
                item.symbol,
                status=JobItemStatus.SUCCEEDED,
                result_ref={
                    "inserted": stored.inserted,
                    "unchanged": stored.unchanged,
                    "revised": stored.revised,
                    "review_required": stored.review_required,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            )
        except asyncio.CancelledError:
            await self._items.release_pending(fence, item.symbol)
            raise
        except TimeoutError:
            await self._fail(fence, item, "HISTORY_PROVIDER_TIMEOUT")
        except HistoryBackfillItemError as exc:
            await self._fail(fence, item, exc.code)
        except AppError as exc:
            await self._fail(fence, item, exc.code)
        except ValueError:
            await self._fail(fence, item, "HISTORY_BARS_INVALID")
        except Exception:
            await self._fail(fence, item, "HISTORY_ITEM_FAILED")

    async def _stop_at_safe_point(
        self, fence: HistoryJobFence, item: HistoryBackfillWorkItem
    ) -> bool:
        control = await self._items.control(fence)
        if control is HistoryBackfillControl.PAUSE_REQUESTED:
            await self._items.release_pending(fence, item.symbol)
            return True
        if control is HistoryBackfillControl.CANCEL_REQUESTED:
            await self._items.finish(fence, item.symbol, status=JobItemStatus.CANCELED)
            return True
        return False

    async def _fail(
        self, fence: HistoryJobFence, item: HistoryBackfillWorkItem, code: str
    ) -> None:
        await self._items.finish(
            fence,
            item.symbol,
            status=JobItemStatus.FAILED,
            error_code=code,
        )

    async def _report_progress(self, fence: HistoryJobFence) -> None:
        summary = await self._items.summary(fence)
        await self._items.report_progress(fence, summary)


def build_history_backfill_handler(
    *,
    provider_factory: Callable[[], HistoryBarsProviderPort],
    store_factory: Callable[[], HistoryBarStorePort],
    items_factory: Callable[[], HistoryJobItemsPort],
    disk_guard_factory: Callable[[], HistoryDiskGuardPort],
    item_timeout_seconds: float = 600,
):
    async def handle(context: JobExecutionContext) -> JobResult:
        try:
            UUID(str(context.config["universe_snapshot_id"]))
            start_date = date.fromisoformat(str(context.config["start_date"]))
            end_date = date.fromisoformat(str(context.config["end_date"]))
            concurrency = int(context.config["concurrency"])
            reason = str(context.config["reason"]).strip()
            if start_date > end_date or not 1 <= concurrency <= 8 or not reason:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return JobResult.failure(
                code="HISTORY_BACKFILL_CONFIG_INVALID",
                message="历史回填任务缺少有效的冻结范围或日期",
                retryable=False,
            )
        executor = HistoryBackfillExecutor(
            provider=provider_factory(),
            store=store_factory(),
            items=items_factory(),
            disk_guard=disk_guard_factory(),
            item_timeout_seconds=item_timeout_seconds,
        )
        try:
            result = await executor.execute(
                context,
                start_date=start_date,
                end_date=end_date,
                concurrency=concurrency,
                reason=reason,
            )
        except AppError as exc:
            return JobResult.failure(
                code=exc.code,
                message=exc.message,
                retryable=exc.status_code >= 500,
            )
        return _job_result(result)

    return handle


def _validate_bars(
    bars,
    *,
    symbol: str,
    start_date: date,
    end_date: date,
):
    rows = tuple(bars)
    if not rows:
        raise HistoryBackfillItemError("HISTORY_BARS_EMPTY", retryable=True)
    seen: set[date] = set()
    for bar in rows:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if (
            bar.symbol != symbol
            or not start_date <= bar.trade_date <= end_date
            or bar.trade_date in seen
            or any(not _positive_finite(value) for value in prices)
            or bar.high < max(prices)
            or bar.low > min(prices)
            or bar.volume < 0
            or not _nonnegative_finite(bar.amount)
            or not bar.source.strip()
        ):
            raise ValueError("invalid historical daily bar")
        seen.add(bar.trade_date)
    return tuple(sorted(rows, key=lambda item: item.trade_date))


def _positive_finite(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def _nonnegative_finite(value: Decimal) -> bool:
    return value.is_finite() and value >= 0


def _store_key(job_id: UUID, symbol: str, start_date: date, end_date: date) -> str:
    return f"history:{job_id}:{symbol}:{start_date.isoformat()}:{end_date.isoformat()}"


def _job_result(result: HistoryBackfillExecutionResult) -> JobResult:
    data = {
        "total": result.total,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "canceled": result.canceled,
        "pending": result.pending,
    }
    if result.control is HistoryBackfillControl.PAUSE_REQUESTED:
        return JobResult(
            success=True,
            code="HISTORY_BACKFILL_PAUSED",
            message="历史回填已在安全点暂停",
            retryable=False,
            data=data,
        )
    if result.control is HistoryBackfillControl.CANCEL_REQUESTED:
        return JobResult(
            success=True,
            code="HISTORY_BACKFILL_CANCELED",
            message="历史回填已在安全点取消",
            retryable=False,
            data=data,
        )
    if result.pending:
        return JobResult.failure(
            code="HISTORY_BACKFILL_RECOVERY_REQUIRED",
            message="历史回填仍有未完成项目",
            retryable=True,
            data=data,
        )
    if result.failed == 0 and result.canceled == 0:
        return JobResult.success_result(data=data, message="历史回填完成")
    if result.succeeded > 0:
        return JobResult(
            success=True,
            code="PARTIAL",
            message="历史回填部分完成",
            retryable=False,
            data=data,
        )
    return JobResult.failure(
        code="HISTORY_BACKFILL_FAILED",
        message="历史回填没有成功项目",
        retryable=False,
        data=data,
    )
