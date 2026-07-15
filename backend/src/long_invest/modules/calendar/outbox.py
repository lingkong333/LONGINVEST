import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.calendar.contracts import CalendarEvent
from long_invest.platform.outbox.models import EventOutbox
from long_invest.platform.outbox.repository import OutboxRepository


class CalendarOutboxAdapter:
    """Public adapter that appends calendar events on the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = OutboxRepository(session)

    async def append(self, event: CalendarEvent) -> EventOutbox:
        dedupe_key = _dedupe_key(event)
        existing = await self._session.scalar(
            select(EventOutbox).where(EventOutbox.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return existing
        return await self._repository.add(
            EventOutbox(
                topic=event.event_type,
                aggregate_type="trading_calendar",
                aggregate_id=event.aggregate_id,
                queue="domain-events",
                payload={
                    "event_type": event.event_type,
                    "aggregate_id": event.aggregate_id,
                    **event.payload,
                },
                dedupe_key=dedupe_key,
            )
        )


def _dedupe_key(event: CalendarEvent) -> str:
    value = f"{event.event_type}:{event.idempotency_key}"
    return f"calendar:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
