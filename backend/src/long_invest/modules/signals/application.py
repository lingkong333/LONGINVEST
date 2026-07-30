from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.monitoring.application import (
    transactional_monitor_subscription_port,
)
from long_invest.modules.providers.contracts import RealtimeQuote
from long_invest.modules.securities.application import TransactionalSignalSecurityPort
from long_invest.modules.signals.contracts import (
    EvaluationReason,
    RealtimeSignalPreparation,
    SignalInput,
)
from long_invest.modules.signals.integrations import (
    TransactionalPositionPort,
    TransactionalQuotePort,
    TransactionalSubscriptionPort,
    TransactionalTargetPort,
    transactional_signal_notification_publisher,
)
from long_invest.modules.signals.outbox import SignalOutbox
from long_invest.modules.signals.repository import SignalRepository
from long_invest.modules.signals.service import SignalService
from long_invest.modules.targets.application import TransactionalTargetSnapshotPort
from long_invest.modules.targets.contracts import TargetStatus
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
        notification_factory: Callable[
            [Any], Any
        ] = transactional_signal_notification_publisher,
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

    async def prepare_realtime(
        self,
        symbols: tuple[str, ...],
        *,
        expected_subscription_versions: Mapping[str, int] | None = None,
    ) -> tuple[RealtimeSignalPreparation, ...]:
        expected = expected_subscription_versions or {}
        prepared: list[RealtimeSignalPreparation] = []
        async with self._database.session() as session:
            subscriptions = transactional_monitor_subscription_port(session)
            targets = TransactionalTargetSnapshotPort(session)
            securities = TransactionalSignalSecurityPort(session)
            for symbol in symbols:
                subscription = await subscriptions.get_subscription_snapshot_by_symbol(
                    symbol
                )
                if subscription is None:
                    continue
                expected_version = expected.get(symbol)
                if (
                    expected_version is not None
                    and subscription.version != expected_version
                ):
                    continue
                target = await targets.get_target_snapshot(
                    subscription.subscription_id
                )
                if target is None or target.status not in {
                    TargetStatus.READY,
                    TargetStatus.STALE,
                }:
                    continue
                security = await securities.get_signal_security(symbol)
                if (
                    security is None
                    or security.security_id != subscription.security_id
                ):
                    continue
                prepared.append(
                    RealtimeSignalPreparation(
                        subscription_id=subscription.subscription_id,
                        security_id=subscription.security_id,
                        symbol=subscription.symbol,
                        security_name=security.name,
                        subscription_version=subscription.version,
                        target_revision_id=target.revision_id,
                        target_version=target.binding_version,
                        target_date=target.target_date,
                        targets=target.values,
                        hysteresis_ratio=subscription.hysteresis_ratio,
                        hysteresis_min=subscription.hysteresis_min,
                    )
                )
        return tuple(prepared)

    async def evaluate_realtime(
        self,
        preparation: RealtimeSignalPreparation,
        quote: RealtimeQuote,
        *,
        scheduled_at: datetime,
        reason: EvaluationReason,
        request_id: str,
        idempotency_key: str,
    ):
        identity = quote.source_identity
        source_identity = (
            {
                "adapter": identity.adapter.value,
                "upstream": identity.upstream.value,
                "interface": identity.interface,
                "capability": identity.capability.value,
                "algorithm_version": identity.algorithm_version,
            }
            if identity is not None
            else None
        )
        price_version = max(
            1,
            int(
                max(scheduled_at.timestamp(), quote.quote_time.timestamp())
                * 1_000_000
            ),
        )
        async with self._database.transaction() as session:
            position = await self._position_factory(session).get_position_snapshot(
                preparation.security_id
            )
            command = SignalInput(
                subscription_id=preparation.subscription_id,
                security_id=preparation.security_id,
                symbol=preparation.symbol,
                security_name=preparation.security_name,
                subscription_version=preparation.subscription_version,
                price=quote.price,
                price_at=quote.quote_time,
                quote_scheduled_at=scheduled_at,
                price_version=price_version,
                target_revision_id=preparation.target_revision_id,
                target_version=preparation.target_version,
                target_date=preparation.target_date,
                targets=preparation.targets,
                quote_source=quote.source.value,
                quote_source_identity=source_identity,
                position_version=position.version if position is not None else 0,
                hysteresis_ratio=preparation.hysteresis_ratio,
                hysteresis_min=preparation.hysteresis_min,
                reason=reason,
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
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
