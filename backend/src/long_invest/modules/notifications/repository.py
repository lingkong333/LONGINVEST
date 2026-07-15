from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
)
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationEvent,
)


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    delivery: NotificationDelivery
    lease_token: UUID


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_event_by_idempotency(
        self,
        idempotency_key: str,
    ) -> NotificationEvent | None:
        return await self._session.scalar(
            select(NotificationEvent).where(
                NotificationEvent.idempotency_key == idempotency_key
            )
        )

    async def persist_event_and_deliveries(
        self,
        event: NotificationEvent,
        deliveries: list[NotificationDelivery],
    ) -> None:
        async with self._session.begin_nested():
            self._session.add(event)
            await self._session.flush([event])
            self._session.add_all(deliveries)
            await self._session.flush(deliveries)

    async def claim_next(
        self,
        *,
        channel: DeliveryChannel,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ClaimedDelivery | None:
        due = or_(
            NotificationDelivery.status == NotificationDeliveryStatus.PENDING,
            and_(
                NotificationDelivery.status == NotificationDeliveryStatus.RETRY_WAIT,
                NotificationDelivery.next_retry_at <= now,
            ),
            and_(
                NotificationDelivery.status
                == NotificationDeliveryStatus.OUTCOME_UNKNOWN,
                NotificationDelivery.next_retry_at <= now,
            ),
        )
        result = await self._session.execute(
            select(NotificationDelivery)
            .where(NotificationDelivery.channel == channel, due)
            .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        delivery = result.scalars().first()
        if delivery is None:
            return None

        lease_token = uuid4()
        delivery.status = NotificationDeliveryStatus.SENDING
        delivery.lease_owner = worker_id
        delivery.lease_token = lease_token
        delivery.lease_expires_at = now + lease_for
        await self._session.flush()
        return ClaimedDelivery(delivery, lease_token)

    async def lock_expired_leases(
        self,
        *,
        channel: DeliveryChannel,
        now: datetime,
        limit: int,
    ) -> list[NotificationDelivery]:
        result = await self._session.execute(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.channel == channel,
                NotificationDelivery.status == NotificationDeliveryStatus.SENDING,
                NotificationDelivery.lease_expires_at <= now,
            )
            .order_by(NotificationDelivery.event_id, NotificationDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())

    async def lock_delivery(self, delivery_id: UUID) -> NotificationDelivery | None:
        return await self._session.scalar(
            select(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id)
            .with_for_update()
        )

    async def get_event(self, event_id: UUID) -> NotificationEvent | None:
        return await self._session.get(NotificationEvent, event_id)

    async def lock_event(self, event_id: UUID) -> NotificationEvent | None:
        return await self._session.scalar(
            select(NotificationEvent)
            .where(NotificationEvent.id == event_id)
            .with_for_update()
        )

    async def list_deliveries(self, event_id: UUID) -> list[NotificationDelivery]:
        result = await self._session.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.event_id == event_id)
            .execution_options(populate_existing=True)
        )
        return list(result.all())

    def add_attempt(self, attempt: NotificationDeliveryAttempt) -> None:
        self._session.add(attempt)

    async def flush(self) -> None:
        await self._session.flush()
