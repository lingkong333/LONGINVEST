from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.targets.outbox import TargetOutbox
from long_invest.modules.targets.repository import TargetRepository
from long_invest.modules.targets.service import TargetService
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError


class TargetApplication:
    def __init__(
        self,
        database: Database,
        *,
        subscription_factory: Callable[[Any], Any],
        repository_factory: Callable[[Any], Any] = TargetRepository,
        audit_factory: Callable[[Any], Any] = AuditService,
        event_factory: Callable[[Any], Any] = TargetOutbox,
        service_factory: Callable[..., Any] = TargetService,
    ) -> None:
        self._database = database
        self._subscription_factory = subscription_factory
        self._repository_factory = repository_factory
        self._audit_factory = audit_factory
        self._event_factory = event_factory
        self._service_factory = service_factory

    async def set_manual(self, command):
        return await self._write("set_manual", command)

    async def restore(self, command):
        return await self._write("restore", command)

    async def _write(self, method, command):
        try:
            async with self._database.transaction() as session:
                service = self._service_factory(
                    self._repository_factory(session),
                    subscriptions=self._subscription_factory(session),
                    audit=self._audit_factory(session),
                    events=self._event_factory(session),
                )
                return await getattr(service, method)(command)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def _backend_unavailable() -> AppError:
    return AppError(
        code="TARGET_BACKEND_UNAVAILABLE",
        message="目标服务暂时不可用",
        status_code=503,
    )
