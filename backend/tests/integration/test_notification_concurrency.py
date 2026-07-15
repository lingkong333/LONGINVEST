import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from long_invest.modules.notifications.channels import ChannelResult
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationEventStatus,
)
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationEvent,
)
from long_invest.modules.notifications.repository import NotificationRepository
from long_invest.modules.notifications.service import (
    ChannelDeliveryTarget,
    DeliveryLease,
    NotificationService,
    PublishNotification,
)
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _command(idempotency_key: str) -> PublishNotification:
    return PublishNotification(
        event_type="notification.test",
        business_event_type="integration.notification_test",
        business_event_id=idempotency_key,
        business_object_type="integration_test",
        business_object_id=idempotency_key,
        severity=None,
        template_variables={"message": "notification concurrency test"},
        template_version="v1",
        targets=(
            ChannelDeliveryTarget(DeliveryChannel.WECOM, 1, "wecom:test"),
            ChannelDeliveryTarget(DeliveryChannel.EMAIL, 1, "email:test"),
        ),
        idempotency_key=idempotency_key,
        request_id=f"req_{uuid4().hex}",
    )


class _PausedPersistRepository(NotificationRepository):
    def __init__(self, session, ready: asyncio.Event, proceed: asyncio.Event) -> None:
        super().__init__(session)
        self._ready = ready
        self._proceed = proceed

    async def persist_event_and_deliveries(self, event, deliveries) -> None:
        self._ready.set()
        await self._proceed.wait()
        await super().persist_event_and_deliveries(event, deliveries)


async def _cleanup(database: Database, idempotency_key: str) -> None:
    event_ids = select(NotificationEvent.id).where(
        NotificationEvent.idempotency_key == idempotency_key
    )
    delivery_ids = select(NotificationDelivery.id).where(
        NotificationDelivery.event_id.in_(event_ids)
    )
    async with database.transaction() as session:
        await session.execute(
            delete(NotificationDeliveryAttempt).where(
                NotificationDeliveryAttempt.delivery_id.in_(delivery_ids)
            )
        )
        await session.execute(
            delete(NotificationDelivery).where(
                NotificationDelivery.event_id.in_(event_ids)
            )
        )
        await session.execute(
            delete(NotificationEvent).where(
                NotificationEvent.idempotency_key == idempotency_key
            )
        )


@pytest.mark.anyio
async def test_concurrent_publish_returns_one_persisted_event() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    key = f"integration-notification-{uuid4().hex}"
    command = _command(key)
    ready = asyncio.Event()
    proceed = asyncio.Event()
    try:
        async with database.session() as first, database.session() as second:
            first_transaction = await first.begin()
            second_transaction = await second.begin()
            first_event = await NotificationService(
                NotificationRepository(first)
            ).publish(command)
            second_publish = asyncio.create_task(
                NotificationService(
                    _PausedPersistRepository(second, ready, proceed)
                ).publish(command)
            )
            await asyncio.wait_for(ready.wait(), timeout=2)
            await first_transaction.commit()
            proceed.set()
            second_event = await asyncio.wait_for(second_publish, timeout=2)
            await second_transaction.commit()

        async with database.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(NotificationEvent)
                .where(NotificationEvent.idempotency_key == key)
            )
        assert second_event.id == first_event.id
        assert count == 1
    finally:
        await _cleanup(database, key)
        await database.dispose()


@pytest.mark.anyio
async def test_two_channels_complete_to_one_delivered_event() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    key = f"integration-notification-{uuid4().hex}"
    now = datetime.now(UTC)
    try:
        async with database.transaction() as session:
            event = await NotificationService(NotificationRepository(session)).publish(
                _command(key)
            )

        leases: list[DeliveryLease] = []
        for channel in (DeliveryChannel.WECOM, DeliveryChannel.EMAIL):
            async with database.transaction() as session:
                claimed = await NotificationRepository(session).claim_next(
                    channel=channel,
                    worker_id=f"integration-{channel.value.lower()}",
                    now=now,
                    lease_for=timedelta(seconds=30),
                )
                assert claimed is not None
                assert claimed.delivery.event_id == event.id
                leases.append(DeliveryLease(claimed.delivery.id, claimed.lease_token))

        async def complete(lease: DeliveryLease) -> bool:
            async with database.transaction() as session:
                return await NotificationService(
                    NotificationRepository(session)
                ).record_result(
                    lease,
                    result=ChannelResult.success(summary="accepted"),
                    started_at=now,
                    finished_at=now + timedelta(milliseconds=25),
                )

        assert await asyncio.gather(*(complete(lease) for lease in leases)) == [
            True,
            True,
        ]

        async with database.session() as session:
            stored = await session.get(NotificationEvent, event.id)
            attempt_count = await session.scalar(
                select(func.count())
                .select_from(NotificationDeliveryAttempt)
                .join(NotificationDelivery)
                .where(NotificationDelivery.event_id == event.id)
            )
        assert stored is not None
        assert stored.status == NotificationEventStatus.DELIVERED
        assert attempt_count == 2
    finally:
        await _cleanup(database, key)
        await database.dispose()
