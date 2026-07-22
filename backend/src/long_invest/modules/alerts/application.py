from __future__ import annotations

from functools import lru_cache
from typing import Any

from long_invest.modules.alerts.integrations import SystemAlertNotificationPublisher
from long_invest.modules.alerts.repository import AlertRepository
from long_invest.modules.alerts.service import AlertService
from long_invest.platform.database.engine import Database, get_database


class AlertApplication:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def read(self, method: str, *args: Any, **kwargs: Any):
        async with self._database.session() as session:
            return await getattr(AlertService(AlertRepository(session)), method)(
                *args, **kwargs
            )

    async def write(self, method: str, *args: Any, **kwargs: Any):
        async with self._database.transaction() as session:
            service = AlertService(
                AlertRepository(session),
                notifications=SystemAlertNotificationPublisher(session),
            )
            return await getattr(service, method)(*args, **kwargs)


@lru_cache
def get_alert_application() -> AlertApplication:
    return AlertApplication(get_database())


def transactional_alert_service(session) -> AlertService:
    return AlertService(
        AlertRepository(session),
        notifications=SystemAlertNotificationPublisher(session),
    )
