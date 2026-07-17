from __future__ import annotations

from typing import Any, Protocol

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
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None: ...


class MonitorScheduleOutboxAdapter:
    def __init__(
        self, session: AsyncSession, writer: TransactionBoundOutboxWriter | None = None
    ) -> None:
        self.session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def changed(self, event: Any) -> None:
        await self._writer.append(
            session=self.session,
            topic="monitor_schedule.changed",
            aggregate_type="monitor_schedule",
            aggregate_id=str(event.schedule_id),
            queue="domain-events",
            payload={
                "event_type": "monitor_schedule.changed",
                "schedule_id": str(event.schedule_id),
                "revision_id": str(event.revision_id),
                "version": event.version,
                "times": list(event.times),
                "reason": event.reason,
                "action": event.action,
            },
            dedupe_key=f"monitor-schedule:{event.schedule_id}:{event.version}:{event.action}",
        )
