from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    HistoryBackfillScope,
)
from long_invest.modules.history_backfills.integrations import (
    SecurityHistoryScopeSnapshotAdapter,
)


class SecurityMaster:
    def __init__(self) -> None:
        self.query = None

    async def freeze_universe(self, query):
        self.query = query
        return snapshot(("000001.SZ", "600000.SH"))

    async def freeze_symbols(self, query):
        self.query = query
        return snapshot(query.symbols)


class Watchlists:
    async def symbols(self, _session, _watchlist_id, *, owner_user_id):
        assert owner_user_id
        return ("600000.SH",)


def snapshot(symbols):
    return SimpleNamespace(
        id=uuid4(),
        master_version=9,
        items=tuple(
            SimpleNamespace(security_id=uuid4(), symbol=symbol) for symbol in symbols
        ),
    )


@pytest.mark.anyio
async def test_all_scope_uses_security_master_universe_snapshot() -> None:
    master = SecurityMaster()
    adapter = SecurityHistoryScopeSnapshotAdapter(
        master_service_factory=lambda _session: master
    )
    frozen = await adapter.freeze(
        object(),
        CreateHistoryBackfill(
            scope=HistoryBackfillScope.ALL,
            start_date=date(2010, 1, 1),
            end_date=date(2020, 12, 31),
            concurrency=4,
        ),
        owner_user_id=uuid4(),
    )
    assert tuple(item.symbol for item in frozen.items) == (
        "000001.SZ",
        "600000.SH",
    )


@pytest.mark.anyio
async def test_watchlist_scope_resolves_symbols_before_freezing() -> None:
    master = SecurityMaster()
    adapter = SecurityHistoryScopeSnapshotAdapter(
        watchlists=Watchlists(),
        master_service_factory=lambda _session: master,
    )
    frozen = await adapter.freeze(
        object(),
        CreateHistoryBackfill(
            scope=HistoryBackfillScope.WATCHLIST,
            watchlist_id=uuid4(),
            start_date=date(2010, 1, 1),
            end_date=date(2020, 12, 31),
            concurrency=4,
        ),
        owner_user_id=uuid4(),
    )
    assert tuple(item.symbol for item in frozen.items) == ("600000.SH",)
