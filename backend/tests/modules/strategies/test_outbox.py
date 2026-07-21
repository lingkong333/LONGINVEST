import asyncio
from types import SimpleNamespace
from uuid import uuid4

from long_invest.modules.strategies.outbox import StrategyOutboxAdapter
from long_invest.modules.strategies.service import StrategyEvent


class Writer:
    def __init__(self):
        self.calls = []

    async def append(self, **kwargs):
        self.calls.append(kwargs)


class Jobs:
    def __init__(self):
        self.calls = []

    async def submit(self, command):
        self.calls.append(command)
        return SimpleNamespace(id=uuid4())


def test_outbox_uses_transaction_session_and_stable_dedupe_key():
    writer = Writer()
    session = SimpleNamespace()
    adapter = StrategyOutboxAdapter(session, writer=writer)
    strategy_id = uuid4()

    asyncio.run(
        adapter.emit(
            StrategyEvent(
                topic="strategy.draft_saved",
                strategy_id=strategy_id,
                dedupe_key="strategy:dedupe",
                payload={"draft_version": 2},
            )
        )
    )

    assert writer.calls == [
        {
            "session": session,
            "topic": "strategy.draft_saved",
            "aggregate_type": "strategy",
            "aggregate_id": str(strategy_id),
            "queue": "domain-events",
            "payload": {
                "event_type": "strategy.draft_saved",
                "draft_version": 2,
            },
            "dedupe_key": "strategy:dedupe",
        }
    ]


def test_publish_request_submits_platform_job_in_same_session():
    writer = Writer()
    jobs = Jobs()
    session = SimpleNamespace()
    adapter = StrategyOutboxAdapter(
        session,
        writer=writer,
        job_service_factory=lambda value: jobs if value is session else None,
    )
    run_id = uuid4()

    asyncio.run(
        adapter.emit(
            StrategyEvent(
                topic="strategy.publish_requested",
                strategy_id=uuid4(),
                dedupe_key="strategy:publish",
                payload={
                    "run_id": str(run_id),
                    "request_id": "request-1",
                    "actor_user_id": "user-1",
                },
            )
        )
    )

    assert writer.calls[0]["queue"] == "domain-events"
    assert len(jobs.calls) == 1
    command = jobs.calls[0]
    assert command.job_type == "STRATEGY_PUBLISH"
    assert command.queue == "strategy"
    assert command.idempotency_key == "strategy:publish"
    assert command.config_snapshot == {"strategy_run_id": str(run_id)}
    assert command.request_id == "request-1"


def test_validation_request_submits_platform_job():
    writer = Writer()
    jobs = Jobs()
    run_id = uuid4()
    adapter = StrategyOutboxAdapter(
        SimpleNamespace(),
        writer=writer,
        job_service_factory=lambda _session: jobs,
    )

    asyncio.run(
        adapter.emit(
            StrategyEvent(
                topic="strategy.validation_requested",
                strategy_id=uuid4(),
                dedupe_key="strategy:validation",
                payload={"validation_run_id": str(run_id)},
            )
        )
    )

    command = jobs.calls[0]
    assert command.job_type == "STRATEGY_VALIDATE"
    assert command.queue == "strategy"
    assert command.config_snapshot == {"validation_run_id": str(run_id)}
