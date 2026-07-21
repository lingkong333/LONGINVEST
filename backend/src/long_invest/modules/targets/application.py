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

    async def list(self, *, page: int = 1, page_size: int = 50):
        return await self._read("list", page=page, page_size=page_size)

    async def get(self, subscription_id):
        return await self._read("get", subscription_id)

    async def history(self, subscription_id, *, page: int = 1, page_size: int = 50):
        return await self._read(
            "history", subscription_id, page=page, page_size=page_size
        )

    async def _read(self, method, *args, **kwargs):
        try:
            async with self._database.session() as session:
                service = self._service_factory(
                    self._repository_factory(session),
                    subscriptions=self._subscription_factory(session),
                    audit=self._audit_factory(session),
                    events=self._event_factory(session),
                )
                return await getattr(service, method)(*args, **kwargs)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

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


class TransactionalTargetSnapshotPort:
    """Public read port for callers already owning the database transaction."""

    def __init__(self, session, *, repository_factory=TargetRepository) -> None:
        self._repository = repository_factory(session)

    async def get_target_snapshot(self, subscription_id):
        service = TargetService(
            self._repository, subscriptions=None, audit=None, events=None
        )
        return await service.get(subscription_id)


def transactional_target_snapshot_port(
    session, **factories
) -> TransactionalTargetSnapshotPort:
    return TransactionalTargetSnapshotPort(session, **factories)
