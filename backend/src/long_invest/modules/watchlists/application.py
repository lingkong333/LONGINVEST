from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.securities.application import (
    SecurityApplication,
    get_security_application,
)
from long_invest.modules.watchlists.contracts import (
    WatchlistBatchInput,
    WatchlistBatchItem,
    WatchlistBatchStatus,
    WatchlistMutation,
    WatchlistView,
)
from long_invest.modules.watchlists.outbox import WatchlistEventAdapter
from long_invest.modules.watchlists.repository import WatchlistRepository
from long_invest.modules.watchlists.service import WatchlistService
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


@dataclass(frozen=True, slots=True)
class WatchlistAuditContext:
    request_id: str
    actor_user_id: str
    session_id: str
    trusted_ip: str


class WatchlistApplication:
    def __init__(
        self,
        database: Database,
        *,
        security_application: SecurityApplication | None = None,
        service_factory: Callable[..., Any] = WatchlistService,
        audit_factory: Callable[..., Any] = AuditService,
        event_factory: Callable[..., Any] = WatchlistEventAdapter,
    ) -> None:
        self._database = database
        self._securities = security_application or get_security_application()
        self._service_factory = service_factory
        self._audit_factory = audit_factory
        self._event_factory = event_factory

    def _service(self, session: Any) -> WatchlistService:
        return self._service_factory(
            WatchlistRepository(session),
            self._audit_factory(session),
            self._event_factory(session),
        )

    async def list(
        self, *, owner_user_id: UUID, include_archived: bool = False
    ) -> tuple[WatchlistView, ...]:
        try:
            async with self._database.session() as session:
                return await self._service(session).list(
                    owner_user_id, include_archived=include_archived
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def get(self, watchlist_id: UUID, *, owner_user_id: UUID) -> WatchlistView:
        try:
            async with self._database.session() as session:
                return await self._service(session).get(
                    watchlist_id, owner_user_id=owner_user_id
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def create(
        self,
        owner_user_id: UUID,
        command: WatchlistMutation,
        *,
        audit_context: WatchlistAuditContext | None = None,
    ) -> WatchlistView:
        return await self._mutate(
            "create", owner_user_id, command, audit_context=audit_context
        )

    async def update(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        command: WatchlistMutation,
        audit_context: WatchlistAuditContext | None = None,
    ) -> WatchlistView:
        return await self._mutate(
            "update",
            watchlist_id,
            owner_user_id=owner_user_id,
            command=command,
            audit_context=audit_context,
        )

    async def archive(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        reason: str,
        idempotency_key: str,
        expected_version: int,
        audit_context: WatchlistAuditContext | None = None,
    ) -> WatchlistView:
        return await self._mutate(
            "archive",
            watchlist_id,
            owner_user_id=owner_user_id,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            audit_context=audit_context,
        )

    async def add_item(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        symbol: str,
        source: str,
        reason: str,
        idempotency_key: str,
        expected_version: int,
        audit_context: WatchlistAuditContext | None = None,
    ):
        try:
            security = await self._securities.resolve_identity(symbol)
            async with self._database.transaction() as session:
                return await self._service(session).add_item(
                    watchlist_id,
                    owner_user_id=owner_user_id,
                    security=security,
                    source=source,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    expected_version=expected_version,
                    audit_context=audit_context,
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def remove_item(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        symbol: str,
        reason: str,
        idempotency_key: str,
        expected_version: int,
        audit_context: WatchlistAuditContext | None = None,
    ):
        try:
            security = await self._securities.resolve_identity(symbol)
            async with self._database.transaction() as session:
                return await self._service(session).remove_item(
                    watchlist_id,
                    owner_user_id=owner_user_id,
                    security_id=security.security_id,
                    symbol=security.symbol,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    expected_version=expected_version,
                    audit_context=audit_context,
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def add_batch(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        batch: WatchlistBatchInput,
        source: str,
        reason: str,
        idempotency_key: str,
        expected_version: int,
        audit_context: WatchlistAuditContext | None = None,
    ) -> tuple[WatchlistBatchItem, ...]:
        results: list[WatchlistBatchItem] = []
        current_version = expected_version
        for index, symbol in enumerate(batch.symbols):
            try:
                result = await self.add_item(
                    watchlist_id,
                    owner_user_id=owner_user_id,
                    symbol=symbol,
                    source=source,
                    reason=reason,
                    idempotency_key=f"{idempotency_key}:{index}",
                    expected_version=current_version,
                    audit_context=audit_context,
                )
                current_version = result.version
                results.append(
                    WatchlistBatchItem(
                        symbol=symbol,
                        status=WatchlistBatchStatus.CREATED
                        if result.created
                        else WatchlistBatchStatus.REUSED,
                        item=result.item,
                    )
                )
            except AppError as exc:
                status = (
                    WatchlistBatchStatus.FAILED
                    if exc.status_code == 503
                    else WatchlistBatchStatus.REJECTED
                )
                results.append(
                    WatchlistBatchItem(
                        symbol=symbol, status=status, error_code=exc.code
                    )
                )
            except Exception:
                results.append(
                    WatchlistBatchItem(
                        symbol=symbol,
                        status=WatchlistBatchStatus.FAILED,
                        error_code="MONITOR_BACKEND_UNAVAILABLE",
                    )
                )
        return tuple(results)

    async def _mutate(self, method: str, *args: Any, **kwargs: Any):
        try:
            async with self._database.transaction() as session:
                return await getattr(self._service(session), method)(*args, **kwargs)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def get_watchlist_application() -> WatchlistApplication:
    return WatchlistApplication(get_database())


def _backend_unavailable() -> AppError:
    return AppError(
        code="MONITOR_BACKEND_UNAVAILABLE",
        message="监控服务暂时不可用",
        status_code=503,
    )
