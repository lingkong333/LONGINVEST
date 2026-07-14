from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.outbox.models import EventOutbox


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: EventOutbox) -> EventOutbox:
        self._session.add(event)
        await self._session.flush()
        return event
