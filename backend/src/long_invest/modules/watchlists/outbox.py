from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.outbox.service import TransactionalOutboxWriter


class TransactionBoundOutboxWriter(Protocol):
    async def append(
        self,
        *,
        session: AsyncSession,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        queue: str,
        payload: dict,
        dedupe_key: str,
    ) -> None: ...


class WatchlistEventAdapter:
    def __init__(
        self, session: AsyncSession, writer: TransactionBoundOutboxWriter | None = None
    ) -> None:
        self._session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def updated(
        self,
        *,
        watchlist_id: UUID,
        action: str,
        symbol: str | None,
        version: int,
        reason: str,
    ) -> None:
        await self._writer.append(
            session=self._session,
            topic="watchlist.updated",
            aggregate_type="watchlist",
            aggregate_id=str(watchlist_id),
            queue="domain-events",
            payload={
                "event_type": "watchlist.updated",
                "watchlist_id": str(watchlist_id),
                "action": action,
                "symbol": symbol,
                "version": version,
                "reason": reason,
            },
            dedupe_key=f"watchlist:{watchlist_id}:{version}:{action}",
        )
