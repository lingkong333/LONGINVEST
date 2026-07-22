from datetime import date
from uuid import uuid4

import pytest

from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    FrozenHistoryScope,
    FrozenHistorySecurity,
    HistoryBackfillScope,
)


def test_single_scope_requires_exactly_one_symbol() -> None:
    with pytest.raises(ValueError, match="一只股票"):
        CreateHistoryBackfill(
            scope=HistoryBackfillScope.SINGLE,
            start_date=date(2010, 1, 1),
            end_date=date(2020, 12, 31),
            concurrency=4,
        )


def test_watchlist_scope_requires_watchlist_id() -> None:
    with pytest.raises(ValueError, match="监控列表"):
        CreateHistoryBackfill(
            scope=HistoryBackfillScope.WATCHLIST,
            start_date=date(2010, 1, 1),
            end_date=date(2020, 12, 31),
            concurrency=4,
        )


def test_date_and_concurrency_are_bounded() -> None:
    with pytest.raises(ValueError, match="开始日期"):
        CreateHistoryBackfill(
            scope=HistoryBackfillScope.ALL,
            start_date=date(2021, 1, 1),
            end_date=date(2020, 12, 31),
            concurrency=4,
        )
    with pytest.raises(ValueError, match="并发数"):
        CreateHistoryBackfill(
            scope=HistoryBackfillScope.ALL,
            start_date=date(2010, 1, 1),
            end_date=date(2020, 12, 31),
            concurrency=9,
        )


def test_frozen_scope_rejects_empty_and_duplicate_items() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        FrozenHistoryScope(snapshot_id=uuid4(), master_version=1, items=())
    security_id = uuid4()
    with pytest.raises(ValueError, match="重复股票"):
        FrozenHistoryScope(
            snapshot_id=uuid4(),
            master_version=1,
            items=(
                FrozenHistorySecurity(security_id, "600000.SH"),
                FrozenHistorySecurity(uuid4(), "600000.SH"),
            ),
        )
