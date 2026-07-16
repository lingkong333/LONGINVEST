from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.outbox.service import TransactionalOutboxWriter


class DailyDataEventWriter:
    def __init__(
        self,
        session: AsyncSession,
        writer: TransactionalOutboxWriter | None = None,
    ) -> None:
        self._session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def append(
        self,
        *,
        topic: str,
        aggregate_id: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None:
        await self._writer.append(
            session=self._session,
            topic=topic,
            aggregate_type=(
                "daily_bar" if topic == "daily_bar.corrected" else "daily_data_batch"
            ),
            aggregate_id=aggregate_id,
            queue="domain-events",
            payload=payload,
            dedupe_key=dedupe_key,
        )
