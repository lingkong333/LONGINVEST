from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    FrozenHistoryScope,
    FrozenHistorySecurity,
    HistoryBackfillScope,
)
from long_invest.modules.securities.contracts import (
    SymbolUniverseQuery,
    UniverseQuery,
)
from long_invest.modules.securities.service import SecurityMasterService
from long_invest.platform.errors import AppError


class HistoryWatchlistSymbolsPort(Protocol):
    async def symbols(
        self,
        session: AsyncSession,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
    ) -> tuple[str, ...]: ...


class SecurityHistoryScopeSnapshotAdapter:
    """通过股票主数据公开服务生成不可变范围快照。"""

    def __init__(
        self,
        *,
        watchlists: HistoryWatchlistSymbolsPort | None = None,
        master_service_factory: Any = SecurityMasterService,
    ) -> None:
        self._watchlists = watchlists
        self._master_service_factory = master_service_factory

    async def freeze(
        self,
        session: AsyncSession,
        command: CreateHistoryBackfill,
        *,
        owner_user_id: UUID,
    ) -> FrozenHistoryScope:
        service = self._master_service_factory(session)
        if command.scope is HistoryBackfillScope.ALL:
            snapshot = await service.freeze_universe(UniverseQuery())
        else:
            symbols = command.symbols
            if command.scope is HistoryBackfillScope.WATCHLIST:
                if self._watchlists is None or command.watchlist_id is None:
                    raise AppError(
                        code="HISTORY_WATCHLIST_SCOPE_NOT_CONFIGURED",
                        message="监控列表范围读取尚未完成生产装配",
                        status_code=503,
                    )
                symbols = await self._watchlists.symbols(
                    session,
                    command.watchlist_id,
                    owner_user_id=owner_user_id,
                )
            snapshot = await service.freeze_symbols(SymbolUniverseQuery(symbols))
        return FrozenHistoryScope(
            snapshot_id=snapshot.id,
            master_version=snapshot.master_version,
            items=tuple(
                FrozenHistorySecurity(item.security_id, item.symbol)
                for item in snapshot.items
            ),
        )
