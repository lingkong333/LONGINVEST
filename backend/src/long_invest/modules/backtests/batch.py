from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from long_invest.modules.backtests.contracts import (
    BacktestBatchSummary,
    BacktestMetricView,
    BacktestReturnDistribution,
    BacktestTradeCountDistribution,
    BacktestUniverseEntry,
)
from long_invest.platform.errors import AppError


@dataclass(frozen=True, slots=True)
class BacktestBatchItemResult:
    entry: BacktestUniverseEntry
    metric: BacktestMetricView | None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if (self.metric is None) == (self.failure_code is None):
            raise ValueError("batch item must contain one metric or failure")


class BacktestBatchItemPort(Protocol):
    async def run_item(
        self, *, task_id: UUID, entry: BacktestUniverseEntry
    ) -> BacktestMetricView: ...


class BacktestBatchRunner:
    def __init__(self, items: BacktestBatchItemPort) -> None:
        self._items = items

    async def run(
        self,
        *,
        task_id: UUID,
        entries: tuple[BacktestUniverseEntry, ...],
        concurrency: int = 4,
    ) -> tuple[tuple[BacktestBatchItemResult, ...], BacktestBatchSummary]:
        if not entries:
            raise ValueError("backtest batch must not be empty")
        if not 1 <= concurrency <= 8:
            raise ValueError("backtest concurrency must be between 1 and 8")
        semaphore = asyncio.Semaphore(concurrency)

        async def execute(entry: BacktestUniverseEntry) -> BacktestBatchItemResult:
            async with semaphore:
                try:
                    metric = await self._items.run_item(task_id=task_id, entry=entry)
                except AppError as exc:
                    return BacktestBatchItemResult(
                        entry=entry, metric=None, failure_code=exc.code
                    )
                except TimeoutError:
                    return BacktestBatchItemResult(
                        entry=entry,
                        metric=None,
                        failure_code="BACKTEST_ITEM_TIMEOUT",
                    )
                except (RuntimeError, ValueError) as exc:
                    failure_code = str(
                        getattr(exc, "code", "BACKTEST_ITEM_FAILED")
                    )
                    return BacktestBatchItemResult(
                        entry=entry,
                        metric=None,
                        failure_code=failure_code,
                    )
                return BacktestBatchItemResult(entry=entry, metric=metric)

        results = tuple(await asyncio.gather(*(execute(entry) for entry in entries)))
        return results, summarize_batch(results)


def summarize_batch(
    results: tuple[BacktestBatchItemResult, ...],
) -> BacktestBatchSummary:
    if not results:
        raise ValueError("backtest batch must not be empty")
    successful = tuple(result for result in results if result.metric is not None)
    total = len(results)
    if not successful:
        return BacktestBatchSummary(
            total_items=total,
            succeeded_items=0,
            failed_items=total,
            success_rate=Decimal(0),
        )

    metrics = tuple(result.metric for result in successful if result.metric is not None)
    returns = sorted(metric.total_return for metric in metrics)
    drawdowns = sorted(
        metric.max_drawdown for metric in metrics
    )
    trade_counts = sorted(
        metric.completed_round_trips for metric in metrics
    )
    ranked = sorted(successful, key=_item_total_return)
    succeeded = len(successful)
    return BacktestBatchSummary(
        total_items=total,
        succeeded_items=succeeded,
        failed_items=total - succeeded,
        success_rate=Decimal(succeeded) / Decimal(total),
        positive_return_ratio=(
            Decimal(sum(value > 0 for value in returns)) / Decimal(succeeded)
        ),
        return_distribution=BacktestReturnDistribution(
            minimum=returns[0],
            percentile_25=_percentile(returns, Decimal("0.25")),
            median=_percentile(returns, Decimal("0.5")),
            percentile_75=_percentile(returns, Decimal("0.75")),
            maximum=returns[-1],
        ),
        median_max_drawdown=_percentile(drawdowns, Decimal("0.5")),
        trade_count_distribution=BacktestTradeCountDistribution(
            minimum=trade_counts[0],
            median=_percentile(
                tuple(Decimal(value) for value in trade_counts), Decimal("0.5")
            ),
            maximum=trade_counts[-1],
        ),
        best_symbol=ranked[-1].entry.symbol,
        worst_symbol=ranked[0].entry.symbol,
    )


def _percentile(values, fraction: Decimal) -> Decimal:
    if len(values) == 1:
        return Decimal(values[0])
    position = fraction * Decimal(len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - Decimal(lower)
    lower_value = Decimal(values[lower])
    upper_value = Decimal(values[upper])
    return lower_value + (upper_value - lower_value) * weight


def _item_total_return(result: BacktestBatchItemResult) -> Decimal:
    assert result.metric is not None
    return result.metric.total_return
