from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.positions.contracts import (
    PositionAuditContext,
    PositionBatchResult,
    PositionSnapshot,
    PositionStatus,
    SetPosition,
)
from long_invest.modules.positions.outbox import PositionOutboxAdapter
from long_invest.modules.positions.repository import PositionRepository
from long_invest.modules.positions.service import PositionService
from long_invest.modules.securities.application import (
    SecurityApplication,
    get_security_application,
)
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


class PositionApplication:
    def __init__(
        self,
        database: Database,
        *,
        security_application: SecurityApplication,
        repository_factory: Callable[..., Any] = PositionRepository,
        audit_factory: Callable[..., Any] = AuditService,
        event_factory: Callable[..., Any] = PositionOutboxAdapter,
        service_factory: Callable[..., Any] = PositionService,
    ) -> None:
        self._database = database
        self._securities = security_application
        self._repository_factory = repository_factory
        self._audit_factory = audit_factory
        self._event_factory = event_factory
        self._service_factory = service_factory

    async def list(self):
        try:
            async with self._database.session() as session:
                return await self._service_factory(
                    self._repository_factory(session)
                ).list()
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def get(self, symbol: str):
        identity = await self._resolve_identity(symbol)
        try:
            async with self._database.session() as session:
                return await self._service_factory(
                    self._repository_factory(session)
                ).get(identity.security_id, symbol=identity.symbol)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def list_page(self, *, page: int, page_size: int):
        try:
            async with self._database.session() as session:
                return await self._service_factory(
                    self._repository_factory(session)
                ).list_page(page=page, page_size=page_size)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def history(self, symbol: str | None = None):
        identity = await self._resolve_identity(symbol) if symbol else None
        try:
            async with self._database.session() as session:
                return await self._service_factory(
                    self._repository_factory(session)
                ).history(identity.security_id if identity else None)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def history_page(
        self,
        symbol: str | None = None,
        *,
        page: int,
        page_size: int,
    ):
        identity = await self._resolve_identity(symbol) if symbol else None
        try:
            async with self._database.session() as session:
                return await self._service_factory(
                    self._repository_factory(session)
                ).history_page(
                    identity.security_id if identity else None,
                    page=page,
                    page_size=page_size,
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def set_status(
        self,
        *,
        symbol: str,
        target: PositionStatus,
        note: str | None,
        reason: str,
        source: str,
        expected_version: int | None,
        idempotency_key: str,
        request_id: str,
        actor_user_id: str,
        session_id: str,
        trusted_ip: str,
    ):
        identity = await self._resolve_identity(symbol)
        context = PositionAuditContext(
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor_user_id=actor_user_id,
            session_id=session_id,
            trusted_ip=trusted_ip,
            reason=reason,
        )
        command = SetPosition(
            security_id=identity.security_id,
            symbol=identity.symbol,
            target=target,
            note=note,
            source=source,
            request_id=request_id,
            idempotency_key=idempotency_key,
            actor_user_id=actor_user_id,
            expected_version=expected_version,
            audit_context=context,
        )
        try:
            async with self._database.transaction() as session:
                return await self._service_factory(
                    self._repository_factory(session),
                    audit_service=self._audit_factory(session),
                    event_sink=self._event_factory(session),
                ).set(command)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def batch_set(
        self,
        *,
        items: tuple[tuple[str, PositionStatus, str | None, int | None], ...],
        source: str,
        reason: str,
        idempotency_key: str,
        request_id: str,
        actor_user_id: str,
        session_id: str,
        trusted_ip: str,
    ) -> tuple[PositionBatchResult, ...]:
        results = []
        for index, (symbol, target, note, expected_version) in enumerate(items):
            try:
                changed = await self.set_status(
                    symbol=symbol,
                    target=target,
                    note=note,
                    reason=reason,
                    source=source,
                    expected_version=expected_version,
                    idempotency_key=f"{idempotency_key}:{index}",
                    request_id=request_id,
                    actor_user_id=actor_user_id,
                    session_id=session_id,
                    trusted_ip=trusted_ip,
                )
                results.append(
                    PositionBatchResult(
                        symbol=symbol,
                        status=(
                            "CHANGED"
                            if changed.code == "POSITION_CHANGED"
                            else "UNCHANGED"
                        ),
                        code=changed.code,
                        position=changed.position,
                    )
                )
            except AppError as exc:
                results.append(
                    PositionBatchResult(
                        symbol=symbol,
                        status="FAILED" if exc.status_code >= 500 else "REJECTED",
                        code=exc.code,
                    )
                )
            except (SQLAlchemyError, TimeoutError):
                results.append(
                    PositionBatchResult(
                        symbol=symbol,
                        status="FAILED",
                        code="POSITION_BACKEND_UNAVAILABLE",
                    )
                )
        return tuple(results)

    async def _resolve_identity(self, symbol: str):
        try:
            value = self._securities.resolve_identity(symbol)
            return await value if inspect.isawaitable(value) else value
        except AppError as exc:
            if exc.code in {"SECURITY_SYMBOL_INVALID", "SECURITY_NOT_FOUND"}:
                raise AppError(
                    code="POSITION_SYMBOL_INVALID",
                    message="股票代码不存在或格式无效",
                    status_code=422,
                ) from exc
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def get_position_application() -> PositionApplication:
    return PositionApplication(
        get_database(), security_application=get_security_application()
    )


async def get_position_snapshot(
    session, security_id, *, repository_factory=PositionRepository
) -> PositionSnapshot:
    row = await repository_factory(session).get_current(security_id)
    if row is None:
        return PositionSnapshot(
            security_id=security_id,
            status=PositionStatus.NOT_HOLDING,
            version=0,
        )
    return PositionSnapshot(
        security_id=row.security_id,
        status=PositionStatus(row.status),
        version=row.version,
    )


def _backend_unavailable() -> AppError:
    return AppError(
        code="POSITION_BACKEND_UNAVAILABLE",
        message="持仓服务暂时不可用",
        status_code=503,
    )
