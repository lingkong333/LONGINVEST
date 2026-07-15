from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.quotes.models import QuoteCycle, QuoteCycleItem
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
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None: ...


class TransactionalQuoteEventAdapter:
    def __init__(
        self, session: AsyncSession, writer: TransactionBoundOutboxWriter | None = None
    ) -> None:
        self.session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def created(self, cycle: QuoteCycle) -> None:
        await self._append(
            cycle,
            "quote_cycle.created",
            {
                "event_type": "quote_cycle.created",
                "cycle_id": str(cycle.id),
                "scheduled_at": cycle.scheduled_at.isoformat(),
                "expected_count": cycle.expected_count,
                "universe_snapshot_id": cycle.universe_snapshot_id,
                "universe_snapshot_version": cycle.universe_snapshot_version,
            },
            f"quote-cycle:{cycle.id}:created",
        )

    async def conflict(self, cycle: QuoteCycle, item: QuoteCycleItem) -> None:
        await self._append(
            cycle,
            "quote_conflict.detected",
            {
                "event_type": "quote_conflict.detected",
                "cycle_id": str(cycle.id),
                "item_id": str(item.id),
                "symbol": item.symbol,
            },
            f"quote-item:{item.id}:conflict",
        )

    async def finalized(
        self, cycle: QuoteCycle, valid_items: list[QuoteCycleItem]
    ) -> None:
        await self._append(
            cycle,
            "quote_cycle.finalized",
            {
                "event_type": "quote_cycle.finalized",
                "cycle_id": str(cycle.id),
                "status": str(cycle.status),
                "valid_item_ids": [str(item.id) for item in valid_items],
            },
            f"quote-cycle:{cycle.id}:finalized",
        )

    async def missing(
        self, cycle: QuoteCycle, abnormal_items: list[QuoteCycleItem]
    ) -> None:
        await self._append(
            cycle,
            "quote_item.missing",
            {
                "event_type": "quote_item.missing",
                "cycle_id": str(cycle.id),
                "items": [
                    {
                        "symbol": item.symbol,
                        "error_code": item.error_code,
                        "status": str(item.status),
                    }
                    for item in abnormal_items
                ],
            },
            f"quote-cycle:{cycle.id}:missing",
        )

    async def _append(
        self, cycle: QuoteCycle, topic: str, payload: dict[str, Any], dedupe_key: str
    ) -> None:
        await self._writer.append(
            session=self.session,
            topic=topic,
            aggregate_type="quote_cycle",
            aggregate_id=str(cycle.id),
            queue="domain-events",
            payload=payload,
            dedupe_key=dedupe_key,
        )
