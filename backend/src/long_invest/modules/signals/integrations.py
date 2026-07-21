from long_invest.modules.monitoring.application import (
    transactional_monitor_subscription_port,
)
from long_invest.modules.notifications.service import (
    transactional_notification_service,
)
from long_invest.modules.positions.application import get_position_snapshot


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


class TransactionalNotificationPublisher:
    def __init__(self, session) -> None:
        self._service = transactional_notification_service(session)

    async def publish(self, notification):
        return await self._service.publish(notification)
