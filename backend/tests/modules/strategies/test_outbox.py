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


def test_publish_request_is_dispatched_to_publish_worker_queue():
    writer = Writer()
    adapter = StrategyOutboxAdapter(SimpleNamespace(), writer=writer)

    asyncio.run(
        adapter.emit(
            StrategyEvent(
                topic="strategy.publish_requested",
                strategy_id=uuid4(),
                dedupe_key="strategy:publish",
                payload={"run_id": str(uuid4())},
            )
        )
    )

    assert writer.calls[0]["queue"] == "strategy-publish"
