from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

import long_invest.bootstrap.stage4_runtime as runtime
from long_invest.modules.backtests.contracts import (
    BacktestCreateRequest,
    BacktestDateRange,
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


def test_runner_identity_is_stable_per_worker_host() -> None:
    assert runtime._worker_id("worker-a") == runtime._worker_id("worker-a")
    assert runtime._worker_id("worker-a") != runtime._worker_id("worker-b")


def _draft_request(draft, *, draft_version: int | None = None):
    return BacktestCreateRequest(
        symbol="600000.SH",
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
