from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

import long_invest.bootstrap.backtest_jobs as jobs_module
from long_invest.modules.backtests.contracts import BacktestMode, BacktestUniverseEntry
from long_invest.platform.jobs.contracts import JobExecutionContext


class Database:
    @asynccontextmanager
    async def transaction(self):
        yield object()


@pytest.mark.anyio
async def test_bulk_coordinator_creates_one_isolated_job_per_frozen_security(
    monkeypatch,
) -> None:
    task_id = uuid4()
    parent_job_id = uuid4()
    entries = (
        BacktestUniverseEntry(
            security_id=uuid4(), symbol="000001.SZ", name="Ping An Bank"
        ),
        BacktestUniverseEntry(
            security_id=uuid4(), symbol="600000.SH", name="SPD Bank"
        ),
    )

    class Application:
        async def get_execution(self, requested_task_id):
            assert requested_task_id == task_id
            return SimpleNamespace(
                task=SimpleNamespace(
                    mode=BacktestMode.WATCHLIST, universe_snapshot=entries
                )
            )

    class Jobs:
        commands = []
        initialized = None

        def __init__(self, _session):
            pass

        async def initialize_items(self, job_id, item_keys):
            assert job_id == parent_job_id
            Jobs.initialized = item_keys

        async def submit(self, command):
            Jobs.commands.append(command)

    monkeypatch.setattr(jobs_module, "build_backtest_application", Application)
    monkeypatch.setattr(jobs_module, "get_database", Database)
    monkeypatch.setattr(jobs_module, "JobService", Jobs)

    result = await jobs_module.backtest_bulk_coordinate(
        JobExecutionContext(
            job_id=parent_job_id,
            fence_token=uuid4(),
            config={
                "backtest_task_id": str(task_id),
                "generation": 1,
                "item_keys": ["600000.SH"],
            },
        )
    )

    assert result.code == "CHILDREN_PENDING"
    assert Jobs.initialized == ("600000.SH",)
    assert [command.queue for command in Jobs.commands] == ["bulk-backtest"]
    assert all(
        command.config_snapshot["linked_item"]["parent_job_id"]
        == str(parent_job_id)
        for command in Jobs.commands
    )


@pytest.mark.anyio
async def test_bulk_coordinator_rejects_single_scope(monkeypatch) -> None:
    class Application:
        async def get_execution(self, _task_id):
            return SimpleNamespace(task=SimpleNamespace(mode=BacktestMode.SINGLE))

    monkeypatch.setattr(jobs_module, "build_backtest_application", Application)
    result = await jobs_module.backtest_bulk_coordinate(
        JobExecutionContext(
            job_id=uuid4(),
            fence_token=uuid4(),
            config={"backtest_task_id": str(uuid4()), "generation": 1},
        )
    )

    assert result.code == "BACKTEST_BULK_SCOPE_INVALID"
    assert result.success is False
