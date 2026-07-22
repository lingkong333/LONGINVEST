from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
)
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class TransactionBoundOutboxWriter(Protocol):
    async def append(
        self,
        *,
        session: AsyncSession,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        queue: str,
        payload: dict,
        dedupe_key: str,
    ) -> None: ...


class NotificationResourceEvents:
    def __init__(
        self,
        session: AsyncSession,
        writer: TransactionBoundOutboxWriter | None = None,
    ) -> None:
        self._session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def event_changed(
        self,
        event: NotificationEvent,
        *,
        change: str,
        dedupe_token: str,
    ) -> None:
        await self._writer.append(
            session=self._session,
            topic="notification.changed.v1",
            aggregate_type="notification_event",
            aggregate_id=str(event.id),
            queue="maintenance",
            payload={
                "notification_event_id": str(event.id),
                "status": _enum_value(event.status),
                "request_id": event.request_id,
                "change": change,
            },
            dedupe_key=(
                f"notification-changed:event:{event.id}:{dedupe_token}:{change}"
            ),
        )

    async def delivery_changed(
        self,
        delivery: NotificationDelivery,
        *,
        request_id: str | None,
        change: str,
        dedupe_token: str,
    ) -> None:
        payload = {
            "notification_delivery_id": str(delivery.id),
            "status": _enum_value(delivery.status),
            "generation": delivery.generation,
            "change": change,
        }
        if request_id is not None:
            payload["request_id"] = request_id
        await self._writer.append(
            session=self._session,
            topic="notification.changed.v1",
            aggregate_type="notification_delivery",
            aggregate_id=str(delivery.id),
            queue="maintenance",
            payload=payload,
            dedupe_key=(
                f"notification-changed:delivery:{delivery.id}:{dedupe_token}:{change}"
            ),
        )


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))
