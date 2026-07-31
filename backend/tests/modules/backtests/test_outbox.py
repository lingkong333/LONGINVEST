import asyncio
from types import SimpleNamespace
from uuid import uuid4

from long_invest.modules.backtests.outbox import (
    BacktestOutboxAdapter,
    _backtest_concurrency,
)
from long_invest.modules.backtests.service import BacktestEvent


class Writer:
    def __init__(self) -> None:
        self.values = []

    async def append(self, **kwargs):
        self.values.append(kwargs)


class Jobs:
    def __init__(self) -> None:
        self.values = []
        self.initialized = None

    async def submit(self, command):
        self.values.append(command)
        return SimpleNamespace(id=uuid4())

    async def initialize_items(self, job_id, item_keys):
        self.initialized = (job_id, item_keys)


def test_backtest_concurrency_accepts_any_positive_integer(monkeypatch) -> None:
    monkeypatch.setenv("LONGINVEST_BACKTEST_CONCURRENCY", "32")

    assert _backtest_concurrency() == 32


def test_created_event_uses_a_postgres_single_backtest_job() -> None:
    async def scenario() -> None:
        writer = Writer()
        jobs = Jobs()
        task_id = uuid4()
        adapter = BacktestOutboxAdapter(
            object(),
            writer=writer,
            job_service_factory=lambda _session: jobs,
        )

        await adapter.emit(
            BacktestEvent(
                topic="backtest.created",
                task_id=task_id,
                payload={"request_id": "req-1", "actor_user_id": "user-1"},
                dedupe_key=f"backtest-created:{task_id}",
            )
        )

        assert writer.values == []
        command = jobs.values[0]
        assert command.job_type == "BACKTEST_SINGLE"
        assert command.module_owner == "backtests"
        assert command.priority == 2
        assert command.recoverable is True
        assert command.config_snapshot == {
            "backtest_task_id": str(task_id),
            "generation": 1,
            "recover": False,
        }

    asyncio.run(scenario())


def test_resumed_event_submits_the_requested_recovery_generation() -> None:
    async def scenario() -> None:
        jobs = Jobs()
        task_id = uuid4()
        adapter = BacktestOutboxAdapter(
            object(),
            writer=Writer(),
            job_service_factory=lambda _session: jobs,
        )

        await adapter.emit(
            BacktestEvent(
                topic="backtest.resumed",
                task_id=task_id,
                payload={
                    "request_id": "req-2",
                    "actor_user_id": "user-2",
                    "generation": 4,
                    "recover": True,
                },
                dedupe_key=f"backtest-resumed:{task_id}:4",
            )
        )

        command = jobs.values[0]
        assert command.idempotency_scope == "backtest-execution"
        assert command.config_snapshot == {
            "backtest_task_id": str(task_id),
            "generation": 4,
            "recover": True,
        }

    asyncio.run(scenario())


def test_market_event_uses_one_postgres_batch_job() -> None:
    async def scenario() -> None:
        jobs = Jobs()
        adapter = BacktestOutboxAdapter(
            object(),
            writer=Writer(),
            job_service_factory=lambda _session: jobs,
        )

        job_id = await adapter.emit(
            BacktestEvent(
                topic="backtest.created",
                task_id=uuid4(),
                payload={
                    "mode": "MARKET",
                    "item_keys": ["000001.SZ", "600000.SH"],
                },
                dedupe_key="market-backtest",
            )
        )

        assert job_id is not None
        assert jobs.values[0].job_type == "BACKTEST_BATCH"
        assert jobs.values[0].module_owner == "backtests"
        assert jobs.values[0].priority == 3
        assert jobs.values[0].recoverable is True
        assert jobs.values[0].config_snapshot["item_keys"] == [
            "000001.SZ",
            "600000.SH",
        ]
        assert jobs.values[0].config_snapshot["concurrency"] == 4
        assert jobs.initialized is None

    asyncio.run(scenario())


def test_result_events_do_not_submit_production_jobs() -> None:
    async def scenario() -> None:
        jobs = Jobs()
        adapter = BacktestOutboxAdapter(
            object(),
            writer=Writer(),
            job_service_factory=lambda _session: jobs,
        )

        await adapter.emit(
            BacktestEvent(
                topic="backtest.item_succeeded",
                task_id=uuid4(),
                payload={},
                dedupe_key="item-succeeded",
            )
        )

        assert jobs.values == []

    asyncio.run(scenario())


def test_worker_recovery_event_does_not_submit_another_job() -> None:
    async def scenario() -> None:
        jobs = Jobs()
        adapter = BacktestOutboxAdapter(
            object(),
            writer=Writer(),
            job_service_factory=lambda _session: jobs,
        )

        await adapter.emit(
            BacktestEvent(
                topic="backtest.resumed",
                task_id=uuid4(),
                payload={"item_id": str(uuid4())},
                dedupe_key="worker-recovery",
            )
        )

        assert jobs.values == []

    asyncio.run(scenario())
