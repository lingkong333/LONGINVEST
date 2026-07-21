from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.strategies.service import StrategyEvent
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class OutboxWriter(Protocol):
    async def append(self, **kwargs) -> None: ...


class StrategyOutboxAdapter:
    def __init__(
        self, session: AsyncSession, writer: OutboxWriter | None = None
    ) -> None:
        self._session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def emit(self, event: StrategyEvent) -> None:
        await self._writer.append(
            session=self._session,
            topic=event.topic,
            aggregate_type="strategy",
            aggregate_id=str(event.strategy_id),
            queue="domain-events",
            payload={"event_type": event.topic, **event.payload},
            dedupe_key=event.dedupe_key,
        )
