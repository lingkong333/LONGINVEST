from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

import long_invest.bootstrap.stage4_runtime as runtime
from long_invest.modules.backtests.contracts import (
    BacktestCreateRequest,
    BacktestDateRange,
    BacktestMode,
)
from long_invest.platform.errors import AppError

RUNNER_DIGEST = "sha256:" + "a" * 64
VALID_SOURCE = '''
STRATEGY_API_VERSION = "1.0"
STRATEGY_META = {
    "name": "fixed target",
    "data_requirements": {
        "adjustment": "qfq",
        "min_bars": 2,
        "max_bars": 1000,
    },
    "parameter_schema": {
        "type": "object",
        "properties": {"window": {"type": "integer", "minimum": 1}},
        "required": ["window"],
        "additionalProperties": False,
    },
}

def calculate_targets(history, params, context):
    return {
        "low_strong": 8,
        "low_watch": 9,
        "high_watch": 11,
        "high_strong": 12,
    }
'''


class Securities:
    def __init__(self) -> None:
        self.security = SimpleNamespace(
            id=uuid4(), symbol="600000.SH", name="SPD Bank"
        )

    async def get(self, symbol: str):
        assert symbol == self.security.symbol
        return self.security


class Strategies:
    def __init__(self, draft) -> None:
        self.draft = draft

    async def get_draft_by_id(self, draft_id):
        assert draft_id == self.draft.id
        return self.draft


def test_draft_backtest_snapshot_is_resolved_from_server_source(monkeypatch) -> None:
    draft = SimpleNamespace(
        id=uuid4(), strategy_id=uuid4(), draft_version=3, source_code=VALID_SOURCE
    )
    securities = Securities()
    resolver = runtime.BacktestSnapshotResolver(
        securities=securities, strategies=Strategies(draft)
    )
    monkeypatch.setattr(
        runtime,
        "get_settings",
        lambda: SimpleNamespace(
            strategy_environment_version="python-3.12",
            strategy_runner_image_digest=RUNNER_DIGEST,
        ),
    )
    request = _draft_request(draft)

    snapshot = asyncio.run(
        resolver.resolve_creation_snapshot(task_id=uuid4(), request=request)
    )

    assert snapshot.draft_source_code == VALID_SOURCE
    assert snapshot.strategy_metadata["name"] == "fixed target"
    assert snapshot.parameter_schema["required"] == ("window",)
    assert snapshot.runner_image_digest == RUNNER_DIGEST
    assert snapshot.universe_snapshot[0].security_id == securities.security.id


def test_draft_backtest_rejects_a_changed_version(monkeypatch) -> None:
    draft = SimpleNamespace(
        id=uuid4(), strategy_id=uuid4(), draft_version=4, source_code=VALID_SOURCE
    )
    resolver = runtime.BacktestSnapshotResolver(
        securities=Securities(), strategies=Strategies(draft)
    )
    monkeypatch.setattr(
        runtime,
        "get_settings",
        lambda: SimpleNamespace(
            strategy_environment_version="python-3.12",
            strategy_runner_image_digest=RUNNER_DIGEST,
        ),
    )

    with pytest.raises(AppError) as raised:
        asyncio.run(
            resolver.resolve_creation_snapshot(
                task_id=uuid4(), request=_draft_request(draft, draft_version=3)
            )
        )

    assert raised.value.code == "BACKTEST_DRAFT_VERSION_CONFLICT"


def test_watchlist_backtest_freezes_only_the_authenticated_owners_scope(
    monkeypatch,
) -> None:
    owner_id = uuid4()
    watchlist_id = uuid4()
    draft = SimpleNamespace(
        id=uuid4(), strategy_id=uuid4(), draft_version=1, source_code=VALID_SOURCE
    )
    securities = Securities()

    class Watchlists:
        async def get(self, requested_id, *, owner_user_id):
            assert requested_id == watchlist_id
            assert owner_user_id == owner_id
            return SimpleNamespace(
                archived=False,
                items=(SimpleNamespace(symbol=securities.security.symbol),),
            )

    resolver = runtime.BacktestSnapshotResolver(
        securities=securities,
        strategies=Strategies(draft),
        watchlists=Watchlists(),
    )
    monkeypatch.setattr(
        runtime,
        "get_settings",
        lambda: SimpleNamespace(
            strategy_environment_version="python-3.12",
            strategy_runner_image_digest=RUNNER_DIGEST,
        ),
    )
    request = _draft_request(
        draft,
        mode=BacktestMode.WATCHLIST,
        symbol=None,
        watchlist_id=watchlist_id,
    )

    snapshot = asyncio.run(
        resolver.resolve_creation_snapshot(
            task_id=uuid4(), request=request, actor_user_id=str(owner_id)
        )
    )

    assert snapshot.mode is BacktestMode.WATCHLIST
    assert tuple(item.symbol for item in snapshot.universe_snapshot) == (
        "600000.SH",
    )


def test_runner_identity_is_stable_per_worker_host() -> None:
    assert runtime._worker_id("worker-a") == runtime._worker_id("worker-a")
    assert runtime._worker_id("worker-a") != runtime._worker_id("worker-b")


def test_adjustment_timeline_is_collected_before_it_is_frozen(monkeypatch) -> None:
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    security_id = uuid4()
    expected = SimpleNamespace(as_of=now)
    calls = []

    class Database:
        @asynccontextmanager
        async def session(self):
            calls.append("query")
            yield object()

    class Collector:
        def __init__(self, database, *, providers, clock):
            assert isinstance(database, Database)
            assert providers == "provider"
            assert clock() == now

        async def collect(self, **command):
            calls.append(("collect", command))

    class Service:
        def __init__(self, repository):
            assert repository == "repository"

        async def get_adjustment_timeline(self, **query):
            calls.append(("freeze", query))
            return expected

    monkeypatch.setattr(runtime, "CorporateActionCollectionApplication", Collector)
    monkeypatch.setattr(
        runtime, "CorporateActionRepository", lambda session: "repository"
    )
    monkeypatch.setattr(runtime, "CorporateActionService", Service)
    preparer = runtime.PersistentAdjustmentTimeline(
        database=Database(), providers="provider", clock=lambda: now
    )
    deadline = now + timedelta(minutes=5)

    result = asyncio.run(
        preparer.prepare_adjustment_timeline(
            security_id=security_id,
            symbol="600000.SH",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            deadline=deadline,
        )
    )

    assert result is expected
    assert calls[0][0] == "collect"
    assert calls[0][1]["security_id"] == security_id
    assert calls[0][1]["symbol"] == "600000.SH"
    assert calls[0][1]["deadline"] == deadline
    assert calls[1] == "query"
    assert calls[2] == (
        "freeze",
        {
            "security_id": security_id,
            "start_date": date(2025, 1, 1),
            "end_date": date(2025, 12, 31),
            "as_of": now,
        },
    )


def _draft_request(
    draft,
    *,
    draft_version: int | None = None,
    mode: BacktestMode = BacktestMode.SINGLE,
    symbol: str | None = "600000.SH",
    watchlist_id=None,
):
    return BacktestCreateRequest(
        mode=mode,
        symbol=symbol,
        watchlist_id=watchlist_id,
        date_range=BacktestDateRange(
            training_start_date=date(2020, 1, 1),
            training_end_date=date(2020, 12, 31),
            test_start_date=date(2021, 1, 1),
            test_end_date=date(2022, 12, 31),
        ),
        draft_id=draft.id,
        draft_version=draft_version or draft.draft_version,
        parameter_snapshot={"window": 20},
        initial_capital=Decimal("100000"),
    )
