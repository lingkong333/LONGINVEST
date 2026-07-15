from __future__ import annotations

from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from long_invest.modules.securities.contracts import UniverseQuery
from long_invest.modules.securities.models import (
    Security,
    SecurityMasterVersion,
    SecurityRevision,
    SecurityUniverseSnapshot,
    SecurityUniverseSnapshotItem,
)
from long_invest.platform.errors import AppError

_SECURITY_MASTER_LOCK_KEY = 0x4C4F4E47494E5653


class SecurityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_symbol(
        self, symbol: str, *, lock: bool = False
    ) -> Security | None:
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
        statement = select(func.count()).select_from(Security)
        return int(await self._session.scalar(statement) or 0)

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

    async def list_all_for_update(self) -> list[Security]:
        result = await self._session.scalars(
            select(Security).order_by(Security.symbol).with_for_update()
        )
        return list(result.all())

    async def lock_master_updates(self) -> None:
        await self._session.execute(
            select(func.pg_advisory_xact_lock(_SECURITY_MASTER_LOCK_KEY))
        )

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
        current = await self._session.scalar(
            select(func.max(SecurityMasterVersion.master_version))
        )
        return int(current or 0)

    async def find_master_import(
        self,
        *,
        source: str,
        idempotency_key: str | None = None,
        source_version: str | None = None,
    ) -> SecurityMasterVersion | None:
        if idempotency_key is not None:
            statement = select(SecurityMasterVersion).where(
                SecurityMasterVersion.idempotency_key == idempotency_key
            )
        elif source_version is not None:
            statement = select(SecurityMasterVersion).where(
                SecurityMasterVersion.source == source,
                SecurityMasterVersion.source_version == source_version,
            )
        else:
            raise ValueError("必须提供幂等键或来源版本")
        return await self._session.scalar(statement)

    async def claim_master_import(
        self, record: SecurityMasterVersion
    ) -> tuple[SecurityMasterVersion, bool]:
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush([record])
        except IntegrityError:
            existing = await self.find_master_import(
                source=record.source,
                idempotency_key=record.idempotency_key,
            )
            if existing is None:
                existing = await self.find_master_import(
                    source=record.source,
                    source_version=record.source_version,
                )
            if existing is None:
                raise AppError(
                    code="SECURITY_MASTER_VERSION_CONFLICT",
                    message="股票主数据版本并发冲突，请重试",
                    status_code=409,
                ) from None
            return existing, False
        return record, True

    def add_master_import(self, record: SecurityMasterVersion) -> None:
        self._session.add(record)

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
