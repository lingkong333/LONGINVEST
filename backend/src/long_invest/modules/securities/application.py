from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.securities.contracts import (
    FrozenSecurity,
    FrozenUniverse,
    ListingStatus,
    Market,
    SecurityAuditContext,
    SecurityIdentity,
    SecurityMasterSnapshot,
    SecurityType,
    SignalSecuritySnapshot,
    SnapshotResult,
    SymbolUniverseQuery,
    UniverseQuery,
    validate_symbol,
)
from long_invest.modules.securities.integrations import (
    SecurityAuditAdapter,
    TransactionalSecurityEventAdapter,
    TransactionBoundOutboxWriter,
)
from long_invest.modules.securities.models import Security
from long_invest.modules.securities.repository import SecurityRepository
from long_invest.modules.securities.service import SecurityMasterService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService


class SecurityApplication:
    def __init__(
        self,
        database: Database,
        *,
        job_service_factory: Callable[..., JobService] = JobService,
        outbox_writer: TransactionBoundOutboxWriter | None = None,
        audit_factory: Callable[..., Any] = SecurityAuditAdapter,
        event_factory: Callable[..., Any] = TransactionalSecurityEventAdapter,
        master_service_factory: Callable[..., Any] = SecurityMasterService,
    ) -> None:
        self._database = database
        self._job_service_factory = job_service_factory
        self._outbox_writer = outbox_writer
        self._audit_factory = audit_factory
        self._event_factory = event_factory
        self._master_service_factory = master_service_factory

    async def list(
        self, *, page: int, page_size: int
    ) -> tuple[list[Security], int]:
        try:
            async with self._database.session() as session:
                repository = SecurityRepository(session)
                return (
                    await repository.list(page=page, page_size=page_size),
                    await repository.count(),
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def search(
        self, *, query: str, page: int, page_size: int
    ) -> tuple[list[Security], int]:
        try:
            async with self._database.session() as session:
                repository = SecurityRepository(session)
                return (
                    await repository.search(query, page=page, page_size=page_size),
                    await repository.count_search(query),
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def get(self, symbol: str) -> Security:
        try:
            validate_symbol(symbol)
        except ValueError as exc:
            raise AppError(
                code="SECURITY_SYMBOL_INVALID",
                message=str(exc),
                status_code=422,
            ) from exc
        try:
            async with self._database.session() as session:
                security = await SecurityRepository(session).get_by_symbol(symbol)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc
        if security is None:
            raise AppError(
                code="SECURITY_NOT_FOUND",
                message="股票不存在",
                status_code=404,
            )
        return security

    async def resolve_identity(self, symbol: str) -> SecurityIdentity:
        try:
            validate_symbol(symbol)
        except ValueError as exc:
            raise AppError(
                code="SECURITY_SYMBOL_INVALID",
                message=str(exc),
                status_code=422,
            ) from exc
        try:
            async with self._database.session() as session:
                security = await SecurityRepository(session).get_by_symbol(symbol)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc
        if security is None:
            raise AppError(
                code="SECURITY_NOT_FOUND",
                message="股票不存在",
                status_code=404,
            )
        return SecurityIdentity(
            security_id=security.id,
            symbol=security.symbol,
            market=Market(security.market),
            security_type=SecurityType(security.security_type),
            listing_status=ListingStatus(security.listing_status),
            is_suspended=security.is_suspended,
            is_st=security.is_st,
            listed_on=security.listed_on,
            delisted_on=security.delisted_on,
            master_version=security.master_version,
        )

    async def refresh(
        self,
        *,
        idempotency_key: str,
        request_id: str,
        created_by_user_id: str,
    ) -> Any:
        command = SubmitJob(
            job_type="SECURITY_MASTER_REFRESH",
            queue="maintenance",
            idempotency_scope="securities:refresh",
            idempotency_key=idempotency_key,
            request_id=request_id,
            config_snapshot={
                "source": "eastmoney",
                "idempotency_key": idempotency_key,
                "request_id": request_id,
                "created_by_user_id": created_by_user_id,
            },
            business_object_type="security_master",
            created_by_user_id=created_by_user_id,
            soft_timeout_seconds=30,
            hard_timeout_seconds=60,
        )
        try:
            async with self._database.transaction() as session:
                return await self._job_service_factory(session).submit(command)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def freeze_symbols(self, symbols: tuple[str, ...]):
        try:
            async with self._database.transaction() as session:
                return await self.freeze_symbols_in_transaction(session, symbols)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def freeze_symbols_in_transaction(self, session, symbols: tuple[str, ...]):
        repository = SecurityRepository(session)
        snapshot = await SecurityMasterService(
            session, repository=repository
        ).freeze_symbols(
            SymbolUniverseQuery(symbols=symbols)
        )
        stored = await repository.get_universe_snapshot(snapshot.id)
        if stored is None:
            raise RuntimeError("saved universe snapshot cannot be reloaded")
        return _frozen_universe(stored)

    async def freeze_universe_in_transaction(self, session):
        repository = SecurityRepository(session)
        snapshot = await SecurityMasterService(
            session, repository=repository
        ).freeze_universe(UniverseQuery())
        stored = await repository.get_universe_snapshot(snapshot.id)
        if stored is None:
            raise RuntimeError("saved universe snapshot cannot be reloaded")
        return _frozen_universe(stored)

    async def frozen_universe(self, snapshot_id: UUID) -> FrozenUniverse:
        try:
            async with self._database.session() as session:
                snapshot = await SecurityRepository(session).get_universe_snapshot(
                    snapshot_id
                )
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc
        if snapshot is None:
            raise AppError(
                code="SECURITY_UNIVERSE_NOT_FOUND",
                message="股票范围快照不存在",
                status_code=404,
            )
        return _frozen_universe(snapshot)

    async def apply_snapshot(
        self,
        snapshot: SecurityMasterSnapshot,
        *,
        audit_context: SecurityAuditContext,
    ) -> SnapshotResult:
        if self._outbox_writer is None:
            raise AppError(
                code="SECURITY_INTEGRATION_UNAVAILABLE",
                message="股票主数据可靠事件写入器不可用",
                status_code=503,
            )
        try:
            async with self._database.transaction() as session:
                audit = self._audit_factory(session)
                events = self._event_factory(session, self._outbox_writer)
                service = self._master_service_factory(
                    session,
                    audit_context=audit_context,
                    audit=audit,
                    events=events,
                )
                return await service.apply_snapshot(snapshot)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def get_security_application() -> SecurityApplication:
    return SecurityApplication(get_database())


class TransactionalSignalSecurityPort:
    """Public security identity reader bound to a caller-owned transaction."""

    def __init__(self, session, *, repository_factory=SecurityRepository) -> None:
        self._repository = repository_factory(session)

    async def get_signal_security(self, symbol: str) -> SignalSecuritySnapshot | None:
        security = await self._repository.get_by_symbol(symbol)
        if security is None:
            return None
        return SignalSecuritySnapshot(
            security_id=security.id,
            symbol=security.symbol,
            name=security.name,
        )


def _frozen_universe(snapshot: Any) -> FrozenUniverse:
    return FrozenUniverse(
        id=snapshot.id,
        master_version=snapshot.master_version,
        items=tuple(
            FrozenSecurity(
                security_id=item.security_id,
                symbol=item.symbol,
                listing_status=ListingStatus(item.listing_status),
                is_suspended=item.is_suspended,
                is_st=item.is_st,
                listed_on=item.listed_on,
                delisted_on=item.delisted_on,
            )
            for item in snapshot.items
        ),
    )


def _backend_unavailable() -> AppError:
    return AppError(
        code="SECURITY_BACKEND_UNAVAILABLE",
        message="股票主数据服务暂时不可用",
        status_code=503,
    )
