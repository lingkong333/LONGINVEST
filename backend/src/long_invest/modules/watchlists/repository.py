from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.watchlists.models import Watchlist, WatchlistItem
from long_invest.platform.audit.service import AuditService
from long_invest.platform.errors import AppError


class WatchlistRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, watchlist_id: UUID, *, lock: bool = False) -> Watchlist | None:
        statement = select(Watchlist).where(Watchlist.id == watchlist_id)
        if lock:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list(
        self, owner_user_id: UUID, *, include_archived: bool = False
    ) -> Sequence[Watchlist]:
        statement = select(Watchlist).where(Watchlist.owner_user_id == owner_user_id)
        if not include_archived:
            statement = statement.where(Watchlist.archived_at.is_(None))
        statement = statement.order_by(
            Watchlist.display_order, Watchlist.created_at, Watchlist.id
        )
        return (await self._session.execute(statement)).scalars().all()

    async def create(self, **values: Any) -> Watchlist:
        record = Watchlist(**values)
        self._session.add(record)
        await self._session.flush()
        return record

    async def find_replay(
        self, idempotency_key: str
    ) -> tuple[str, UUID, dict[str, Any]] | None:
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended(idempotency_key, 0))
            )
        )
        event = await AuditService(self._session).find_by_idempotency(idempotency_key)
        if event is None or not event.after_summary:
            return None
        request_hash = event.after_summary.get("request_hash")
        if not isinstance(request_hash, str):
            return None
        try:
            return request_hash, UUID(event.object_id), event.after_summary
        except ValueError:
            return None

    async def lock_security_memberships(self, security_id: UUID) -> None:
        lock_key = f"watchlists:security-memberships:{security_id}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )

    async def update_version(
        self, watchlist_id: UUID, *, expected_version: int, **values: Any
    ) -> Watchlist:
        statement = (
            update(Watchlist)
            .where(Watchlist.id == watchlist_id, Watchlist.version == expected_version)
            .values(**values, version=Watchlist.version + 1, updated_at=func.now())
        )
        result = await self._session.execute(statement)
        if result.rowcount != 1:
            raise _version_conflict()
        record = await self.get(watchlist_id)
        if record is None:
            raise _not_found()
        return record

    async def archive(self, watchlist_id: UUID, *, expected_version: int) -> Watchlist:
        return await self.update_version(
            watchlist_id,
            expected_version=expected_version,
            archived_at=func.now(),
        )

    async def list_items(self, watchlist_id: UUID) -> Sequence[WatchlistItem]:
        statement = (
            select(WatchlistItem)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .order_by(WatchlistItem.created_at, WatchlistItem.id)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_item(
        self, watchlist_id: UUID, security_id: UUID
    ) -> WatchlistItem | None:
        statement = select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.security_id == security_id,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def add_item(
        self, watchlist_id: UUID, *, security_id: UUID, symbol: str, source: str
    ) -> WatchlistItem:
        item = WatchlistItem(
            watchlist_id=watchlist_id,
            security_id=security_id,
            symbol=symbol,
            source=source,
        )
        self._session.add(item)
        await self._session.flush()
        return item

    async def remove_item(
        self, watchlist_id: UUID, security_id: UUID
    ) -> WatchlistItem | None:
        item = await self.get_item(watchlist_id, security_id)
        if item is None:
            return None
        await self._session.execute(
            delete(WatchlistItem).where(WatchlistItem.id == item.id)
        )
        return item

    async def count_memberships(self, security_id: UUID) -> int:
        statement = (
            select(func.count(WatchlistItem.id))
            .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
            .where(
                WatchlistItem.security_id == security_id,
                Watchlist.archived_at.is_(None),
            )
        )
        return int((await self._session.execute(statement)).scalar_one())


def _not_found() -> AppError:
    return AppError(
        code="WATCHLIST_NOT_FOUND", message="监控分组不存在", status_code=404
    )


def _version_conflict() -> AppError:
    return AppError(
        code="WATCHLIST_VERSION_CONFLICT",
        message="分组已被其他请求修改，请刷新后重试",
        status_code=409,
    )
