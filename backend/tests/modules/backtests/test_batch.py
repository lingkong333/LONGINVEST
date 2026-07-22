import asyncio
from decimal import Decimal
from uuid import uuid4

from long_invest.modules.backtests.batch import BacktestBatchRunner
from long_invest.modules.backtests.contracts import (
    BacktestMetricView,
    BacktestUniverseEntry,
)
from long_invest.platform.errors import AppError


class ItemRunner:
    def __init__(self, failed_symbol: str | None = None) -> None:
        self.failed_symbol = failed_symbol
        self.active = 0
        self.maximum_active = 0

    async def run_item(self, *, task_id, entry):
        del task_id
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if entry.symbol == self.failed_symbol:
            raise AppError(code="ITEM_DATA_INVALID", message="invalid", status_code=409)
        number = Decimal(entry.symbol[:2]) / Decimal("100")
        return _metric(total_return=number)


def test_batch_runner_isolates_item_failure_and_limits_concurrency() -> None:
    async def scenario() -> None:
        entries = tuple(_entry(f"{index:06d}.SZ") for index in range(1, 7))
        item_runner = ItemRunner(failed_symbol=entries[2].symbol)

        results, summary = await BacktestBatchRunner(item_runner).run(
            task_id=uuid4(), entries=entries, concurrency=2
        )

        assert len(results) == 6
        assert results[2].failure_code == "ITEM_DATA_INVALID"
        assert summary.succeeded_items == 5
        assert summary.failed_items == 1
        assert item_runner.maximum_active <= 2
        assert "portfolio_return" not in summary.model_dump()
        assert "portfolio_equity" not in summary.model_dump()

    asyncio.run(scenario())


def test_batch_summary_reports_per_stock_distribution_only() -> None:
    async def scenario() -> None:
        entries = (_entry("000001.SZ"), _entry("600000.SH"))
        _, summary = await BacktestBatchRunner(ItemRunner()).run(
            task_id=uuid4(), entries=entries
        )

        assert summary.success_rate == 1
        assert summary.return_distribution is not None
        assert summary.best_symbol == "600000.SH"
        assert summary.worst_symbol == "000001.SZ"

    asyncio.run(scenario())


def _entry(symbol: str) -> BacktestUniverseEntry:
    return BacktestUniverseEntry(
        security_id=uuid4(), symbol=symbol, name=f"股票{symbol}"
    )


def _metric(*, total_return: Decimal) -> BacktestMetricView:
    return BacktestMetricView(
        item_id=uuid4(),
        ending_equity="100000",
        total_return=total_return,
        realized_return=total_return,
        annualized_return=total_return,
        max_drawdown="0.1",
        volatility="0.2",
        sharpe_ratio="1",
        completed_round_trips=1,
        winning_trades=1,
        losing_trades=0,
        breakeven_trades=0,
        win_rate="1",
        average_trade_return=total_return,
        maximum_trade_gain=total_return,
        maximum_trade_loss=None,
        average_holding_trade_days="5",
        longest_holding_trade_days=5,
        capital_exposure_ratio="0.5",
        open_position_at_end=False,
        unfilled_order_count=0,
    )
