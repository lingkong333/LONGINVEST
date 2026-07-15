import hashlib

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.calendar.contracts import CalendarEvent
from long_invest.platform.errors import AppError
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


class CalendarOutboxAdapter:
    """Public adapter that appends calendar events on the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: CalendarEvent) -> EventOutbox:
        dedupe_key = _dedupe_key(event)
        stored = await self._session.scalar(
            insert(EventOutbox)
            .values(
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
                status=OutboxStatus.PENDING,
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(EventOutbox)
        )
        if stored is not None:
            return stored
        existing = await self._session.scalar(
            select(EventOutbox).where(EventOutbox.dedupe_key == dedupe_key)
        )
        if existing is None:
            raise AppError(
                code="CALENDAR_OUTBOX_CONFLICT_UNRESOLVED",
                message="可靠事件去重结果暂时不可见",
                status_code=503,
            )
        return existing


def _dedupe_key(event: CalendarEvent) -> str:
    value = f"{event.event_type}:{event.idempotency_key}"
    return f"calendar:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
