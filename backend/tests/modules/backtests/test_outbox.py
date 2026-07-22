import asyncio
from uuid import uuid4

from long_invest.modules.backtests.outbox import BacktestOutboxAdapter
from long_invest.modules.backtests.service import BacktestEvent


class Writer:
    def __init__(self) -> None:
        self.values = []

    async def append(self, **kwargs):
        self.values.append(kwargs)


class Jobs:
    def __init__(self) -> None:
        self.values = []

    async def submit(self, command):
        self.values.append(command)


def test_created_event_uses_an_isolated_single_backtest_queue() -> None:
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

        assert writer.values[0]["queue"] == "domain-events"
        command = jobs.values[0]
        assert command.job_type == "BACKTEST_SINGLE"
        assert command.queue == "backtest-single"
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
