from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.positions.contracts import PositionEvent
from long_invest.platform.errors import AppError
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


class PositionOutboxAdapter:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: PositionEvent) -> EventOutbox:
        stored = await self._session.scalar(
            insert(EventOutbox)
            .values(
                topic=event.event_type,
                aggregate_type="position",
                aggregate_id=event.aggregate_id,
                queue="domain-events",
                payload={"event_type": event.event_type, **event.payload},
                dedupe_key=event.dedupe_key,
                status=OutboxStatus.PENDING,
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(EventOutbox)
        )
        if stored is not None:
            return stored
        existing = await self._session.scalar(
            select(EventOutbox).where(EventOutbox.dedupe_key == event.dedupe_key)
        )
        if existing is None:
            raise AppError(
                code="POSITION_OUTBOX_CONFLICT_UNRESOLVED",
                message="持仓可靠事件去重结果暂时不可见",
                status_code=503,
            )
        return existing
