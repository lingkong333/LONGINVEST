from __future__ import annotations

from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from long_invest.modules.securities.contracts import UniverseQuery
from long_invest.modules.securities.models import (
    Security,
    SecurityRevision,
    SecurityUniverseSnapshot,
    SecurityUniverseSnapshotItem,
)


class SecurityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_symbol(self, symbol: str, *, lock: bool = False) -> Security | None:
        statement = select(Security).where(Security.symbol == symbol)
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def list(self, *, page: int, page_size: int) -> list[Security]:
        _validate_page(page, page_size)
        result = await self._session.scalars(
            select(Security)
            .order_by(Security.symbol)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return list(result.all())

    async def count(self) -> int:
        return int(await self._session.scalar(select(func.count()).select_from(Security)) or 0)

    async def search(
        self, query: str, *, page: int, page_size: int
    ) -> list[Security]:
        _validate_page(page, page_size)
        pattern = f"%{query.strip()}%"
        result = await self._session.scalars(
            select(Security)
            .where(
                or_(Security.symbol.ilike(pattern), Security.name.ilike(pattern))
            )
            .order_by(Security.symbol)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return list(result.all())

    async def count_search(self, query: str) -> int:
        pattern = f"%{query.strip()}%"
        statement = select(func.count()).select_from(Security).where(
            or_(Security.symbol.ilike(pattern), Security.name.ilike(pattern))
        )
        return int(await self._session.scalar(statement) or 0)

    async def get_many(self, symbols: Iterable[str]) -> dict[str, Security]:
        unique_symbols = sorted(set(symbols))
        if not unique_symbols:
            return {}
        result = await self._session.scalars(
            select(Security).where(Security.symbol.in_(unique_symbols))
        )
        return {item.symbol: item for item in result.all()}

    async def list_for_universe(self, query: UniverseQuery) -> list[Security]:
        statement = select(Security).where(
            Security.market.in_([item.value for item in query.markets]),
            Security.security_type.in_([item.value for item in query.security_types]),
            Security.listing_status.in_(
                [item.value for item in query.listing_statuses]
            ),
        )
        if not query.include_st:
            statement = statement.where(Security.is_st.is_(False))
        if not query.include_suspended:
            statement = statement.where(Security.is_suspended.is_(False))
        result = await self._session.scalars(statement.order_by(Security.symbol))
        return list(result.all())

    async def get_universe_snapshot(
        self, snapshot_id: UUID
    ) -> SecurityUniverseSnapshot | None:
        statement = (
            select(SecurityUniverseSnapshot)
            .where(SecurityUniverseSnapshot.id == snapshot_id)
            .options(selectinload(SecurityUniverseSnapshot.items))
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    def add_security(self, security: Security) -> None:
        self._session.add(security)

    def add_revision(self, revision: SecurityRevision) -> None:
        self._session.add(revision)

    async def next_revision_no(self, security_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(SecurityRevision.revision_no)).where(
                SecurityRevision.security_id == security_id
            )
        )
        return int(current or 0) + 1

    async def current_master_version(self) -> int:
        current = await self._session.scalar(select(func.max(Security.master_version)))
        return int(current or 0)

    async def save_universe_snapshot(
        self,
        snapshot: SecurityUniverseSnapshot,
        items: Sequence[SecurityUniverseSnapshotItem],
    ) -> None:
        self._session.add(snapshot)
        await self._session.flush([snapshot])
        self._session.add_all(items)
        await self._session.flush(list(items))

    async def flush(self) -> None:
        await self._session.flush()


def _validate_page(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 200:
        raise ValueError("分页参数超出有效范围")
