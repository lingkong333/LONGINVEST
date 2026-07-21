from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.signals.integrations import (
    TransactionalNotificationPublisher,
    TransactionalPositionPort,
    TransactionalQuotePort,
    TransactionalSubscriptionPort,
    TransactionalTargetPort,
)
from long_invest.modules.signals.outbox import SignalOutbox
from long_invest.modules.signals.repository import SignalRepository
from long_invest.modules.signals.service import SignalService
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.service import JobService


class SignalApplication:
    def __init__(
        self,
        database: Database,
        *,
        repository_factory: Callable[[Any], Any] = SignalRepository,
        subscription_factory: Callable[[Any], Any] = TransactionalSubscriptionPort,
        target_factory: Callable[[Any], Any] = TransactionalTargetPort,
        quote_factory: Callable[[Any], Any] = TransactionalQuotePort,
        position_factory: Callable[[Any], Any] = TransactionalPositionPort,
        notification_factory: Callable[[Any], Any] = TransactionalNotificationPublisher,
        audit_factory: Callable[[Any], Any] = AuditService,
        event_factory: Callable[[Any], Any] = SignalOutbox,
        job_factory: Callable[[Any], Any] = JobService,
        service_factory: Callable[..., Any] = SignalService,
    ) -> None:
        self._database = database
        self._repository_factory = repository_factory
        self._subscription_factory = subscription_factory
        self._target_factory = target_factory
        self._quote_factory = quote_factory
        self._position_factory = position_factory
        self._notification_factory = notification_factory
        self._audit_factory = audit_factory
        self._event_factory = event_factory
        self._job_factory = job_factory
        self._service_factory = service_factory

    async def evaluate(self, command):
        try:
            async with self._database.transaction() as session:
                service = self._service_factory(
                    self._repository_factory(session),
                    subscriptions=self._subscription_factory(session),
                    targets=self._target_factory(session),
                    quotes=self._quote_factory(session),
                    positions=self._position_factory(session),
                    notifications=self._notification_factory(session),
                    events=self._event_factory(session),
                )
                return await service.evaluate(command)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise AppError(
                code="SIGNAL_BACKEND_UNAVAILABLE",
                message="信号服务暂时不可用",
                status_code=503,
            ) from exc

    async def reset(self, command):
        return await self._write("reset", command)

    async def reevaluate(self, command):
        return await self._write("reevaluate", command)

    async def list_states(self, *, page: int = 1, page_size: int = 50):
        return await self._read("list_states", page=page, page_size=page_size)

    async def list_evaluations(self, *, page: int = 1, page_size: int = 50):
        return await self._read("list_evaluations", page=page, page_size=page_size)

    async def list_events(self, *, page: int = 1, page_size: int = 50):
        return await self._read("list_events", page=page, page_size=page_size)

    async def get_state(self, subscription_id):
        return await self._read("get_state", subscription_id)

    async def get_evaluation(self, evaluation_id):
        return await self._read("get_evaluation", evaluation_id)

    async def get_event(self, event_id):
        return await self._read("get_event", event_id)

    async def _read(self, method, *args, **kwargs):
        try:
            async with self._database.session() as session:
                service = self._build_service(session)
                return await getattr(service, method)(*args, **kwargs)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def _write(self, method, command):
        try:
            async with self._database.transaction() as session:
                service = self._build_service(session, mutations=True)
                return await getattr(service, method)(command)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    def _build_service(self, session, *, mutations=False):
        ports = {}
        if mutations:
            ports = {
                "audit": self._audit_factory(session),
                "events": self._event_factory(session),
                "jobs": self._job_factory(session),
            }
        return self._service_factory(
            self._repository_factory(session),
            subscriptions=self._subscription_factory(session),
            targets=self._target_factory(session),
            quotes=self._quote_factory(session),
            positions=self._position_factory(session),
            notifications=self._notification_factory(session),
            **ports,
        )


def _backend_unavailable() -> AppError:
    return AppError(
        code="SIGNAL_BACKEND_UNAVAILABLE",
        message="信号服务暂时不可用",
        status_code=503,
    )
