from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


class TransactionalOutboxWriter:
    """Append a deduplicated event using the caller's database transaction."""

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
    ) -> None:
        await session.execute(
            insert(EventOutbox)
            .values(
                topic=topic,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                queue=queue,
                payload=payload,
                dedupe_key=dedupe_key,
                status=OutboxStatus.PENDING,
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
