from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

from long_invest.modules.history_backfills.contracts import (
    HistoryBackfillItemError,
    HistoryBackfillWorkItem,
    HistoryBarsBundle,
    HistoryBarsProviderPort,
    HistoryBarStorePort,
    HistoryDiskGuardPort,
)
from long_invest.modules.securities.application import SecurityApplication
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobProgress,
    JobResult,
    JobStatus,
)
from long_invest.platform.jobs.postgres_service import PostgresJobService

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HistoryItemOutcome:
    symbol: str
    security_id: UUID
    success: bool
    error_code: str | None = None
    retryable: bool = False
    inserted: int = 0
    unchanged: int = 0
    revised: int = 0
    review_required: int = 0
    qfq_rows: int = 0
    anomalies: tuple[dict[str, str], ...] = ()


class PostgresHistoryBackfillJob:
    def __init__(
        self,
        database: Any,
        *,
        provider_factory: Callable[[], HistoryBarsProviderPort],
        store_factory: Callable[[], HistoryBarStorePort],
        disk_guard_factory: Callable[[], HistoryDiskGuardPort],
        item_timeout_seconds: float = 600,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if item_timeout_seconds <= 0:
            raise ValueError("item timeout must be positive")
        self._database = database
        self._provider_factory = provider_factory
        self._store_factory = store_factory
        self._disk_guard = disk_guard_factory()
        self._item_timeout_seconds = item_timeout_seconds
        self._now = now_provider or (lambda: datetime.now(UTC))

    async def __call__(self, context: JobExecutionContext) -> JobResult:
        config = _history_config(context.config)
        if config is None:
            return JobResult.failure(
                code="HISTORY_BACKFILL_CONFIG_INVALID",
                message="历史回填任务缺少有效的冻结范围或日期",
                retryable=False,
            )
        (
            snapshot_id,
            start_date,
            end_date,
            concurrency,
            reason,
            complete_mode,
            provider_code,
        ) = config
        try:
            frozen = await SecurityApplication(self._database).frozen_universe(
                snapshot_id
            )
        except AppError as error:
            return JobResult.failure(
                code=error.code,
                message=error.message,
                retryable=error.status_code >= 500,
            )

        checkpoint = dict(context.checkpoint)
        retry_items = tuple(str(item) for item in checkpoint.get("retry_items", ()))
        items_by_symbol = {item.symbol: item for item in frozen.items}
        if retry_items:
            if len(retry_items) != len(set(retry_items)) or any(
                symbol not in items_by_symbol for symbol in retry_items
            ):
                return _checkpoint_invalid()
            selected = tuple(items_by_symbol[symbol] for symbol in retry_items)
        else:
            selected = frozen.items
        try:
            base_succeeded = int(checkpoint.get("base_succeeded", 0))
            progress_total = int(checkpoint.get("original_total", len(selected)))
        except (TypeError, ValueError):
            return _checkpoint_invalid()
        if (
            base_succeeded < 0
            or progress_total < len(selected)
            or base_succeeded + len(selected) > progress_total
        ):
            return _checkpoint_invalid()
        state = _restore_history_state(checkpoint, total=len(selected))
        if state is None:
            return _checkpoint_invalid()
        cursor, succeeded, failures, anomalies, counts = state

        while cursor < len(selected):
            status = await self._report(
                context,
                cursor=cursor,
                succeeded=succeeded,
                failures=failures,
                anomalies=anomalies,
                counts=counts,
                retry_items=retry_items,
                base_succeeded=base_succeeded,
                progress_total=progress_total,
                message=(
                    f"正在处理第 {cursor + 1} 至 "
                    f"{min(cursor + concurrency, len(selected))} 只股票"
                ),
            )
            if status in {JobStatus.PAUSED, JobStatus.CANCELED}:
                return _history_stopped(
                    status,
                    base_succeeded + cursor,
                    progress_total,
                    failures,
                )
            if not await self._disk_guard.is_backfill_safe():
                status = await self._report(
                    context,
                    cursor=cursor,
                    succeeded=succeeded,
                    failures=failures,
                    anomalies=anomalies,
                    counts=counts,
                    retry_items=retry_items,
                    base_succeeded=base_succeeded,
                    progress_total=progress_total,
                    message="磁盘使用率达到安全上限，历史回填已暂停",
                    pause=True,
                )
                return _history_stopped(
                    status or JobStatus.PAUSED,
                    base_succeeded + cursor,
                    progress_total,
                    failures,
                )

            group = selected[cursor : cursor + concurrency]
            outcomes = await asyncio.gather(
                *(
                    self._process_item(
                        context,
                        HistoryBackfillWorkItem(
                            item.security_id,
                            item.symbol,
                            provider_code=provider_code,
                        ),
                        start_date=(
                            max(start_date, item.listed_on)
                            if complete_mode and item.listed_on is not None
                            else start_date
                        ),
                        end_date=(
                            min(end_date, item.delisted_on)
                            if complete_mode and item.delisted_on is not None
                            else end_date
                        ),
                        concurrency=concurrency,
                        reason=reason,
                    )
                    for item in group
                )
            )
            for outcome in outcomes:
                if outcome.success:
                    succeeded += 1
                    if outcome.anomalies:
                        anomalies.append(
                            {
                                "security_id": str(outcome.security_id),
                                "symbol": outcome.symbol,
                                "rows": list(outcome.anomalies),
                            }
                        )
                    for name in counts:
                        counts[name] += int(getattr(outcome, name))
                else:
                    failures.append(
                        {
                            "security_id": str(outcome.security_id),
                            "symbol": outcome.symbol,
                            "error_code": outcome.error_code
                            or "HISTORY_ITEM_FAILED",
                            "retryable": outcome.retryable,
                        }
                    )
            cursor += len(group)
            status = await self._report(
                context,
                cursor=cursor,
                succeeded=succeeded,
                failures=failures,
                anomalies=anomalies,
                counts=counts,
                retry_items=retry_items,
                base_succeeded=base_succeeded,
                progress_total=progress_total,
                message=f"已处理 {cursor}/{len(selected)} 只股票",
            )
            if status in {JobStatus.PAUSED, JobStatus.CANCELED}:
                return _history_stopped(
                    status,
                    base_succeeded + cursor,
                    progress_total,
                    failures,
                )

        data = {
            "total": progress_total,
            "succeeded": base_succeeded + succeeded,
            "failed": len(failures),
            "canceled": 0,
            "pending": 0,
            "inserted": counts["inserted"],
            "unchanged": counts["unchanged"],
            "revised": counts["revised"],
            "review_required": counts["review_required"],
            "qfq_rows": counts["qfq_rows"],
            "failed_items": [item["symbol"] for item in failures],
            "failure_details": failures,
            "anomalous": len(anomalies),
            "anomaly_details": anomalies,
        }
        if not failures:
            return JobResult.success_result(data=data, message="历史回填完成")
        if base_succeeded + succeeded:
            return JobResult(
                success=True,
                code="PARTIAL",
                message="历史回填部分完成",
                retryable=False,
                data=data,
            )
        return JobResult.failure(
            code="HISTORY_BACKFILL_FAILED",
            message="历史回填没有成功股票",
            retryable=False,
            data=data,
        )

    async def _process_item(
        self,
        context: JobExecutionContext,
        item: HistoryBackfillWorkItem,
        *,
        start_date: date,
        end_date: date,
        concurrency: int,
        reason: str,
    ) -> HistoryItemOutcome:
        try:
            deadline = self._now() + timedelta(seconds=self._item_timeout_seconds)
            async with asyncio.timeout(self._item_timeout_seconds):
                bundle = await self._provider_factory().fetch(
                    item,
                    start_date=start_date,
                    end_date=end_date,
                    deadline=deadline,
                    concurrency=concurrency,
                )
            bundle = _validate_bundle(
                bundle,
                symbol=item.symbol,
                start_date=start_date,
                end_date=end_date,
            )
            stored = await self._store_factory().store(
                item,
                bundle,
                idempotency_key=_store_key(
                    context.job_id, item.symbol, start_date, end_date
                ),
                reason=reason,
            )
            return HistoryItemOutcome(
                item.symbol,
                item.security_id,
                True,
                inserted=stored.inserted,
                unchanged=stored.unchanged,
                revised=stored.revised,
                review_required=stored.review_required,
                qfq_rows=stored.qfq_rows,
                anomalies=bundle.anomalies,
            )
        except TimeoutError:
            return _failed_outcome(item, "HISTORY_PROVIDER_TIMEOUT", True)
        except HistoryBackfillItemError as error:
            return _failed_outcome(item, error.code, error.retryable)
        except AppError as error:
            return _failed_outcome(item, error.code, error.status_code >= 500)
        except ValueError:
            return _failed_outcome(item, "HISTORY_BARS_INVALID", False)
        except Exception as error:
            logger.exception(
                "history_backfill_item_failed",
                job_id=str(context.job_id),
                symbol=item.symbol,
                error_type=type(error).__name__,
            )
            return _failed_outcome(item, "HISTORY_ITEM_FAILED", False)

    async def _report(
        self,
        context: JobExecutionContext,
        *,
        cursor: int,
        succeeded: int,
        failures: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        counts: dict[str, int],
        retry_items: tuple[str, ...],
        base_succeeded: int,
        progress_total: int,
        message: str,
        pause: bool = False,
    ) -> JobStatus | None:
        checkpoint = {
            "cursor": cursor,
            "succeeded": succeeded,
            "failures": failures,
            "anomalies": anomalies,
            "counts": counts,
        }
        if retry_items:
            checkpoint["retry_items"] = list(retry_items)
            checkpoint["original_total"] = progress_total
            checkpoint["base_succeeded"] = base_succeeded
        async with self._database.transaction() as session:
            return await PostgresJobService(session).report_progress(
                context.job_id,
                context.fence_token,
                progress=JobProgress(
                    completed=base_succeeded + cursor,
                    total=progress_total,
                    message=message,
                ),
                checkpoint=checkpoint,
                lease_duration=timedelta(seconds=60),
                now=self._now(),
                pause=pause,
            )


def build_postgres_history_backfill_handler(
    database: Any,
    *,
    provider_factory: Callable[[], HistoryBarsProviderPort],
    store_factory: Callable[[], HistoryBarStorePort],
    disk_guard_factory: Callable[[], HistoryDiskGuardPort],
    item_timeout_seconds: float = 600,
):
    return PostgresHistoryBackfillJob(
        database,
        provider_factory=provider_factory,
        store_factory=store_factory,
        disk_guard_factory=disk_guard_factory,
        item_timeout_seconds=item_timeout_seconds,
    )


def _history_config(
    config: Any,
) -> tuple[UUID, date, date, int, str, bool, str | None] | None:
    try:
        snapshot_id = UUID(str(config["universe_snapshot_id"]))
        start_date = date.fromisoformat(str(config["start_date"]))
        end_date = date.fromisoformat(str(config["end_date"]))
        concurrency = int(config["concurrency"])
        reason = str(config["reason"]).strip()
        complete_mode = str(config.get("date_mode", "ADVANCED")) == "COMPLETE"
        provider_code = config.get("provider_code")
        if provider_code is not None:
            provider_code = str(provider_code).strip().upper() or None
        if start_date > end_date or concurrency < 1 or not reason:
            return None
        return (
            snapshot_id,
            start_date,
            end_date,
            concurrency,
            reason,
            complete_mode,
            provider_code,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _restore_history_state(
    checkpoint: dict[str, Any], *, total: int
) -> tuple[
    int,
    int,
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, int],
] | None:
    try:
        cursor = int(checkpoint.get("cursor", 0))
        succeeded = int(checkpoint.get("succeeded", 0))
        failures = [dict(item) for item in checkpoint.get("failures", ())]
        anomalies = [dict(item) for item in checkpoint.get("anomalies", ())]
        raw_counts = dict(checkpoint.get("counts", {}))
        counts = {
            key: int(raw_counts.get(key, 0))
            for key in (
                "inserted",
                "unchanged",
                "revised",
                "review_required",
                "qfq_rows",
            )
        }
    except (TypeError, ValueError):
        return None
    if (
        cursor < 0
        or cursor > total
        or succeeded < 0
        or succeeded + len(failures) != cursor
        or any(value < 0 for value in counts.values())
        or any(
            not str(item.get("symbol", "")).strip()
            or not str(item.get("error_code", "")).strip()
            for item in failures
        )
    ):
        return None
    return cursor, succeeded, failures, anomalies, counts


def _failed_outcome(
    item: HistoryBackfillWorkItem, code: str, retryable: bool
) -> HistoryItemOutcome:
    return HistoryItemOutcome(
        item.symbol,
        item.security_id,
        False,
        error_code=code,
        retryable=retryable,
    )


def _checkpoint_invalid() -> JobResult:
    return JobResult.failure(
        code="HISTORY_BACKFILL_CHECKPOINT_INVALID",
        message="历史回填任务检查点无效",
        retryable=False,
    )


def _history_stopped(
    status: JobStatus,
    cursor: int,
    total: int,
    failures: list[dict[str, object]],
) -> JobResult:
    return JobResult(
        success=True,
        code=f"JOB_{status.value}",
        message="历史回填已在安全点停止",
        retryable=False,
        data={
            "total": total,
            "succeeded": cursor - len(failures),
            "failed": len(failures),
            "canceled": 0,
            "pending": total - cursor,
            "failed_items": [item["symbol"] for item in failures],
        },
    )


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
    valid = []
    anomalies: list[dict[str, str]] = []
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
            anomalies.append(
                {
                    "trade_date": bar.trade_date.isoformat(),
                    "error_code": "HISTORY_BAR_OHLC_INVALID",
                }
            )
            continue
        seen.add(bar.trade_date)
        valid.append(bar)
    if not valid:
        raise HistoryBackfillItemError("HISTORY_BARS_ALL_INVALID", retryable=False)
    return tuple(sorted(valid, key=lambda item: item.trade_date)), tuple(anomalies)


def _validate_bundle(
    bundle: HistoryBarsBundle,
    *,
    symbol: str,
    start_date: date,
    end_date: date,
) -> HistoryBarsBundle:
    unadjusted, raw_anomalies = _validate_bars(
        bundle.unadjusted,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
    )
    qfq, qfq_anomalies = _validate_bars(
        bundle.qfq,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
    )
    invalid_dates = {
        item["trade_date"] for item in (*raw_anomalies, *qfq_anomalies)
    }
    invalid_dates.update(
        item["trade_date"]
        for item in bundle.anomalies
        if item.get("trade_date")
    )
    unadjusted = tuple(
        item for item in unadjusted if item.trade_date.isoformat() not in invalid_dates
    )
    qfq = tuple(
        item for item in qfq if item.trade_date.isoformat() not in invalid_dates
    )
    if not unadjusted or not qfq:
        raise HistoryBackfillItemError("HISTORY_BARS_ALL_INVALID", retryable=False)
    raw_by_date = {item.trade_date: item for item in unadjusted}
    qfq_dates = tuple(item.trade_date for item in qfq)
    raw_suffix = tuple(
        item.trade_date for item in unadjusted if item.trade_date >= qfq[0].trade_date
    )
    if qfq_dates != raw_suffix:
        raise HistoryBackfillItemError("HISTORY_QFQ_WINDOW_MISMATCH", retryable=False)
    anchor = raw_by_date[qfq[-1].trade_date]
    if qfq[-1].close != anchor.close:
        raise HistoryBackfillItemError("HISTORY_QFQ_ANCHOR_MISMATCH", retryable=False)
    return HistoryBarsBundle(
        unadjusted=unadjusted,
        qfq=qfq,
        provider_contract_version=bundle.provider_contract_version,
        anomalies=bundle.anomalies
        + tuple(
            {**item, "price_mode": "UNADJUSTED"} for item in raw_anomalies
        )
        + tuple({**item, "price_mode": "QFQ"} for item in qfq_anomalies),
    )


def _positive_finite(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def _nonnegative_finite(value: Decimal) -> bool:
    return value.is_finite() and value >= 0


def _store_key(job_id: UUID, symbol: str, start_date: date, end_date: date) -> str:
    return f"history:{job_id}:{symbol}:{start_date.isoformat()}:{end_date.isoformat()}"
