from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from long_invest.modules.quotes.contracts import QuoteCycleStatus
from long_invest.modules.quotes.models import QuoteCycle, QuoteCycleItem


class QuoteCycleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim_cycle(self, cycle: QuoteCycle) -> tuple[QuoteCycle, bool]:
        try:
            async with self.session.begin_nested():
                self.session.add(cycle)
                await self.session.flush()
            return cycle, True
        except IntegrityError:
            existing = await self.session.scalar(
                select(QuoteCycle)
                .options(selectinload(QuoteCycle.items))
                .where(
                    QuoteCycle.idempotency_scope == cycle.idempotency_scope,
                    QuoteCycle.idempotency_key == cycle.idempotency_key,
                )
            )
            if existing is None:
                raise
            return existing, False

    async def get_with_items(self, cycle_id: UUID) -> QuoteCycle | None:
        return await self.session.scalar(
            select(QuoteCycle)
            .options(selectinload(QuoteCycle.items))
            .where(QuoteCycle.id == cycle_id)
        )

    async def get_for_finalize(self, cycle_id: UUID) -> QuoteCycle | None:
        return await self.get_for_update(cycle_id)

    async def get_for_update(self, cycle_id: UUID) -> QuoteCycle | None:
        return await self.session.scalar(
            select(QuoteCycle)
            .options(selectinload(QuoteCycle.items))
            .where(QuoteCycle.id == cycle_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def get_item_for_update(
        self, cycle_id: UUID, symbol: str
    ) -> QuoteCycleItem | None:
        return await self.session.scalar(
            select(QuoteCycleItem)
            .where(
                QuoteCycleItem.cycle_id == cycle_id,
                QuoteCycleItem.symbol == symbol,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def list(
        self,
        *,
        status: QuoteCycleStatus | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> list[QuoteCycle]:
        statement = select(QuoteCycle).options(selectinload(QuoteCycle.items))
        if status is not None:
            statement = statement.where(QuoteCycle.status == status)
        rows = await self.session.scalars(
            statement.order_by(QuoteCycle.scheduled_at.desc(), QuoteCycle.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.all())

    async def count(self, *, status: QuoteCycleStatus | None = None) -> int:
        statement = select(func.count()).select_from(QuoteCycle)
        if status is not None:
            statement = statement.where(QuoteCycle.status == status)
        return int(await self.session.scalar(statement) or 0)

    async def list_items(
        self, cycle_id: UUID, *, page: int = 1, page_size: int = 200
    ) -> list[QuoteCycleItem]:
        rows = await self.session.scalars(
            select(QuoteCycleItem)
            .where(QuoteCycleItem.cycle_id == cycle_id)
            .order_by(QuoteCycleItem.symbol)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.all())

    async def find_expired(self, now: datetime, limit: int) -> list[QuoteCycle]:
        rows = await self.session.scalars(
            select(QuoteCycle)
            .where(
                QuoteCycle.status.in_(
                    (QuoteCycleStatus.FETCHING, QuoteCycleStatus.FINALIZING)
                ),
                QuoteCycle.deadline_at <= now,
            )
            .order_by(QuoteCycle.deadline_at, QuoteCycle.id)
            .limit(limit)
        )
        return list(rows.all())

    async def flush(self) -> None:
        await self.session.flush()
