from sqlalchemy import func, select

from long_invest.platform.database.engine import Database
from long_invest.platform.events.contracts import (
    SUPPORTED_TOPICS,
    StoredResourceEvent,
)
from long_invest.platform.outbox.models import EventOutbox


class PostgresEventSource:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def latest_sequence(self) -> int:
        statement = select(func.max(EventOutbox.stream_sequence)).where(
            EventOutbox.topic.in_(SUPPORTED_TOPICS)
        )
        async with self._database.session() as session:
            return int(await session.scalar(statement) or 0)

    async def contains_sequence(self, sequence: int) -> bool:
        statement = select(EventOutbox.stream_sequence).where(
            EventOutbox.stream_sequence == sequence,
            EventOutbox.topic.in_(SUPPORTED_TOPICS),
        )
        async with self._database.session() as session:
            return await session.scalar(statement) is not None

    async def fetch_after(
        self, sequence: int, *, limit: int
    ) -> tuple[StoredResourceEvent, ...]:
        statement = (
            select(
                EventOutbox.stream_sequence,
                EventOutbox.topic,
                EventOutbox.aggregate_id,
            )
            .where(
                EventOutbox.stream_sequence > sequence,
                EventOutbox.topic.in_(SUPPORTED_TOPICS),
            )
            .order_by(EventOutbox.stream_sequence)
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = (await session.execute(statement)).all()
        return tuple(
            StoredResourceEvent(
                sequence=row.stream_sequence,
                topic=row.topic,
                aggregate_id=row.aggregate_id,
            )
            for row in rows
        )
