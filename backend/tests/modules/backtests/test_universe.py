import asyncio
from uuid import uuid4

import pytest

from long_invest.modules.backtests.contracts import (
    BacktestMode,
    BacktestUniverseEntry,
    BacktestUniverseSelection,
)
from long_invest.modules.backtests.universe import BacktestUniverseFreezer
from long_invest.platform.errors import AppError


class UniverseSource:
    def __init__(self, entries=()) -> None:
        self.entries = tuple(entries)

    async def get_single(self, symbol):
        return next(entry for entry in self.entries if entry.symbol == symbol)

    async def list_watchlist(self, _watchlist_id):
        return self.entries

    async def list_market(self):
        return self.entries


def test_watchlist_freeze_deduplicates_and_sorts_the_snapshot() -> None:
    async def scenario() -> None:
        first = _entry("600000.SH")
        second = _entry("000001.SZ")
        freezer = BacktestUniverseFreezer(
            UniverseSource((first, second, first))
        )

        frozen = await freezer.freeze(
            BacktestUniverseSelection(
                mode=BacktestMode.WATCHLIST, watchlist_id=uuid4()
            )
        )

        assert [entry.symbol for entry in frozen.entries] == [
            "000001.SZ",
            "600000.SH",
        ]
        assert frozen.survivor_bias_disclosed is False

    asyncio.run(scenario())


def test_market_freeze_is_stable_and_discloses_survivor_bias() -> None:
    async def scenario() -> None:
        entries = (_entry("600000.SH"), _entry("000001.SZ"))
        freezer = BacktestUniverseFreezer(UniverseSource(entries))
        selection = BacktestUniverseSelection(mode=BacktestMode.MARKET)

        first = await freezer.freeze(selection)
        second = await freezer.freeze(selection)

        assert first == second
        assert first.survivor_bias_disclosed is True

    asyncio.run(scenario())


def test_empty_batch_scope_is_rejected() -> None:
    async def scenario() -> None:
        freezer = BacktestUniverseFreezer(UniverseSource())
        with pytest.raises(AppError) as captured:
            await freezer.freeze(
                BacktestUniverseSelection(
                    mode=BacktestMode.WATCHLIST, watchlist_id=uuid4()
                )
            )
        assert captured.value.code == "BACKTEST_UNIVERSE_EMPTY"

    asyncio.run(scenario())


def _entry(symbol: str) -> BacktestUniverseEntry:
    return BacktestUniverseEntry(
        security_id=uuid4(), symbol=symbol, name=f"股票{symbol}"
    )
