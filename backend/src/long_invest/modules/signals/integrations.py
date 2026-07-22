from typing import Protocol

from long_invest.modules.monitoring.application import (
    transactional_monitor_subscription_port,
)
from long_invest.modules.notifications.contracts import NotificationEventStatus
from long_invest.modules.notifications.service import (
    ChannelDeliveryTarget,
    PublishNotification,
    transactional_notification_service,
)
from long_invest.modules.notifications.targets import DynamicNotificationTargetResolver
from long_invest.modules.positions.application import get_position_snapshot
from long_invest.modules.positions.contracts import PositionStatus
from long_invest.modules.quotes.application import transactional_quote_signal_port
from long_invest.modules.signals.contracts import (
    NotificationClass,
    SignalNotificationRequest,
)
from long_invest.modules.targets.application import transactional_target_snapshot_port
from long_invest.platform.errors import AppError

_TEMPLATE_TYPES = {
    NotificationClass.LOW: "signal.low",
    NotificationClass.LOW_CLEARED: "signal.low_cleared",
    NotificationClass.HIGH: "signal.high",
    NotificationClass.HIGH_CLEARED: "signal.high_cleared",
}


class SignalNotificationPolicyUnavailable(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="SIGNAL_NOTIFICATION_POLICY_UNAVAILABLE",
            message="信号通知策略暂时不可用",
            status_code=503,
        )


class SignalNotificationTargetResolver(Protocol):
    async def resolve_targets(
        self,
        notification: SignalNotificationRequest,
    ) -> tuple[ChannelDeliveryTarget, ...]: ...


class TransactionalSubscriptionPort:
    def __init__(self, session) -> None:
        self._port = transactional_monitor_subscription_port(session)

    async def lock(self, subscription_id):
        return await self._port.lock(subscription_id)

    async def get_subscription_snapshot(self, subscription_id):
        return await self._port.get_subscription_snapshot(subscription_id)

    async def switch_to_manual(self, **kwargs):
        return await self._port.switch_to_manual(**kwargs)


class TransactionalPositionPort:
    def __init__(self, session) -> None:
        self._session = session

    async def get_position_snapshot(self, security_id):
        return await get_position_snapshot(self._session, security_id)


class TransactionalTargetPort:
    def __init__(self, session) -> None:
        self._port = transactional_target_snapshot_port(session)

    async def get_target_snapshot(self, subscription_id):
        return await self._port.get_target_snapshot(subscription_id)


class TransactionalQuotePort:
    def __init__(self, session) -> None:
        self._port = transactional_quote_signal_port(session)

    async def get_quote_snapshot(self, *, item_id, cycle_id):
        return await self._port.get_quote_snapshot(
            item_id=item_id,
            cycle_id=cycle_id,
        )


class TransactionalNotificationPublisher:
    def __init__(
        self,
        session,
        *,
        target_resolver: SignalNotificationTargetResolver | None = None,
    ) -> None:
        self._service = transactional_notification_service(session)
        self._target_resolver = target_resolver

    async def publish(self, notification: SignalNotificationRequest):
        targets: tuple[ChannelDeliveryTarget, ...] = ()
        if notification.eligible:
            if self._target_resolver is None:
                raise SignalNotificationPolicyUnavailable()
            targets = tuple(await self._target_resolver.resolve_targets(notification))

        command = PublishNotification(
            event_type=_TEMPLATE_TYPES[notification.notification_class],
            business_event_type="signal.transitioned",
            business_event_id=str(notification.event_id),
            business_object_type="monitor_subscription",
            business_object_id=str(notification.subscription_id),
            severity=None,
            template_variables=_template_variables(notification),
            template_version="v1",
            targets=targets,
            idempotency_key=notification.idempotency_key,
            request_id=notification.request_id,
            eligibility_status=(
                NotificationEventStatus.ELIGIBLE
                if notification.eligible
                else NotificationEventStatus.SUPPRESSED
            ),
            suppression_reason=notification.suppression_reason,
        )
        return await self._service.publish(command)


def transactional_signal_notification_publisher(session):
    return TransactionalNotificationPublisher(
        session,
        target_resolver=DynamicNotificationTargetResolver(session),
    )


def _template_variables(notification: SignalNotificationRequest) -> dict[str, object]:
    return {
        "symbol": notification.symbol,
        "name": notification.security_name,
        "previous_state": notification.before_zone.value,
        "current_state": notification.after_zone.value,
        "price": str(notification.price),
        "quote_time": notification.price_at.isoformat(),
        "targets": {
            "low_strong": str(notification.targets.low_strong),
            "low_watch": str(notification.targets.low_watch),
            "high_watch": str(notification.targets.high_watch),
            "high_strong": str(notification.targets.high_strong),
        },
        "target_version": notification.target_version,
        "target_date": notification.target_date.isoformat(),
        "target_stale": notification.target_stale,
        "holding": notification.position_status is PositionStatus.HOLDING,
        "reason": notification.reason.value,
    }
