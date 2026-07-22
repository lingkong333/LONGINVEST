from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.modules.notifications.admin import (
    AdminPage,
    NotificationAdminError,
    NotificationAdminRepository,
    NotificationAdminService,
    aggregate_current_event_status,
)
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
)


def make_event() -> NotificationEvent:
    return NotificationEvent(
        id=uuid4(),
        event_type="signal.high",
        business_event_type="signal.transitioned",
        business_event_id="signal-1",
        business_object_type="subscription",
        business_object_id="subscription-1",
        template_variables={"symbol": "600000.SH"},
        status=NotificationEventStatus.FAILED,
        eligibility_status=NotificationEventStatus.ELIGIBLE,
        effective_channels=[DeliveryChannel.WECOM],
        template_version="v1",
        idempotency_key=f"notification-{uuid4()}",
        content_hash="1" * 64,
        request_id="request-1",
    )


def make_delivery(
    event: NotificationEvent,
    *,
    status: NotificationDeliveryStatus,
    channel: DeliveryChannel = DeliveryChannel.WECOM,
    generation: int = 1,
) -> NotificationDelivery:
    return NotificationDelivery(
        id=uuid4(),
        event_id=event.id,
        generation=generation,
        channel=channel,
        config_version=3,
        target_fingerprint=f"{channel.value.lower()}:primary",
        status=status,
        attempt_count=2,
        unknown_compensation_count=0,
        deterministic_message_id=(
            f"notification:{event.id}:{channel.value}:{generation}"
        ),
    )


class FakeRepository:
    def __init__(
        self,
        event: NotificationEvent,
        deliveries: list[NotificationDelivery],
    ) -> None:
        self.event = event
        self.deliveries = deliveries
        self.added: list[NotificationDelivery] = []
        self.flush_count = 0

    async def lock_delivery(self, delivery_id):
        return next((item for item in self.deliveries if item.id == delivery_id), None)

    async def lock_event(self, event_id):
        return self.event if self.event.id == event_id else None

    async def list_event_deliveries(self, event_id):
        return [item for item in self.deliveries if item.event_id == event_id]

    def add_delivery(self, delivery):
        self.deliveries.append(delivery)
        self.added.append(delivery)

    async def flush(self):
        self.flush_count += 1


@pytest.mark.anyio
async def test_event_list_has_bounded_pagination_and_stable_order() -> None:
    session = AsyncMock()
    session.scalar.return_value = 42
    scalars = Mock()
    scalars.all.return_value = []
    session.scalars.return_value = scalars

    page = await NotificationAdminRepository(session).list_events(
        page=2,
        page_size=20,
        status=NotificationEventStatus.FAILED,
        event_type="signal.high",
    )

    statement = session.scalars.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "ORDER BY NOTIFICATION_EVENT.CREATED_AT DESC" in sql
    assert "NOTIFICATION_EVENT.ID DESC" in sql
    assert "LIMIT" in sql and "OFFSET" in sql
    assert page == AdminPage(items=(), page=2, page_size=20, total=42)


@pytest.mark.anyio
async def test_attempt_list_is_bounded_and_newest_attempt_is_first() -> None:
    session = AsyncMock()
    session.scalar.return_value = 0
    scalars = Mock()
    scalars.all.return_value = []
    session.scalars.return_value = scalars

    await NotificationAdminRepository(session).list_attempts(
        uuid4(),
        page=1,
        page_size=50,
    )

    statement = session.scalars.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "NOTIFICATION_DELIVERY_ATTEMPT.DELIVERY_ID" in sql
    assert "ATTEMPT_NO DESC" in sql
    assert "LIMIT" in sql


@pytest.mark.anyio
async def test_retry_failed_delivery_creates_new_generation_without_mutating_old() -> (
    None
):
    event = make_event()
    failed = make_delivery(event, status=NotificationDeliveryStatus.FAILED)
    repository = FakeRepository(event, [failed])

    result = await NotificationAdminService(repository).retry_delivery(failed.id)

    assert result.changed is True
    assert failed.status == NotificationDeliveryStatus.FAILED
    assert failed.attempt_count == 2
    assert result.delivery.generation == 2
    assert result.delivery.status == NotificationDeliveryStatus.PENDING
    assert result.delivery.attempt_count == 0
    assert result.delivery.config_version == failed.config_version
    assert result.delivery.target_fingerprint == failed.target_fingerprint
    assert result.delivery.deterministic_message_id.endswith(":WECOM:2")
    assert event.status == NotificationEventStatus.DISPATCHED
    assert repository.flush_count == 1


@pytest.mark.anyio
async def test_repeating_retry_on_old_generation_returns_explicit_conflict() -> None:
    event = make_event()
    failed = make_delivery(event, status=NotificationDeliveryStatus.FAILED)
    current = make_delivery(
        event,
        status=NotificationDeliveryStatus.PENDING,
        generation=2,
    )
    repository = FakeRepository(event, [failed, current])

    with pytest.raises(NotificationAdminError) as exc_info:
        await NotificationAdminService(repository).retry_delivery(failed.id)

    assert exc_info.value.code == "NOTIFICATION_DELIVERY_RETRY_SUPERSEDED"
    assert repository.added == []


@pytest.mark.anyio
async def test_outcome_unknown_cannot_use_ordinary_manual_retry() -> None:
    event = make_event()
    unknown = make_delivery(
        event,
        status=NotificationDeliveryStatus.OUTCOME_UNKNOWN,
    )
    repository = FakeRepository(event, [unknown])

    with pytest.raises(NotificationAdminError) as exc_info:
        await NotificationAdminService(repository).retry_delivery(unknown.id)

    assert exc_info.value.code == "NOTIFICATION_DELIVERY_OUTCOME_UNKNOWN"
    assert repository.added == []


@pytest.mark.anyio
async def test_cancel_pending_is_idempotent_and_aggregates_current_generations() -> (
    None
):
    event = make_event()
    old_failed = make_delivery(event, status=NotificationDeliveryStatus.FAILED)
    current = make_delivery(
        event,
        status=NotificationDeliveryStatus.PENDING,
        generation=2,
    )
    repository = FakeRepository(event, [old_failed, current])
    service = NotificationAdminService(repository)

    first = await service.cancel_delivery(current.id)
    second = await service.cancel_delivery(current.id)

    assert first.changed is True
    assert second.changed is False
    assert current.status == NotificationDeliveryStatus.CANCELED
    assert current.error_code == "CANCELED_BY_USER"
    assert event.status == NotificationEventStatus.CANCELED
    assert repository.flush_count == 1


@pytest.mark.anyio
async def test_cancel_rejects_sending_delivery() -> None:
    event = make_event()
    sending = make_delivery(event, status=NotificationDeliveryStatus.SENDING)
    sending.lease_token = uuid4()
    sending.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
    repository = FakeRepository(event, [sending])

    with pytest.raises(NotificationAdminError) as exc_info:
        await NotificationAdminService(repository).cancel_delivery(sending.id)

    assert exc_info.value.code == "NOTIFICATION_DELIVERY_NOT_CANCELABLE"
    assert sending.status == NotificationDeliveryStatus.SENDING


@pytest.mark.anyio
async def test_failed_batch_retries_valid_items_and_isolates_invalid_items() -> None:
    event = make_event()
    failed = make_delivery(event, status=NotificationDeliveryStatus.FAILED)
    sent = make_delivery(
        event,
        status=NotificationDeliveryStatus.SENT,
        channel=DeliveryChannel.EMAIL,
    )
    repository = FakeRepository(event, [failed, sent])

    result = await NotificationAdminService(repository).retry_failed_batch(
        [failed.id, sent.id, failed.id, UUID(int=0)]
    )

    assert len(result.retried) == 1
    assert result.retried[0].generation == 2
    assert [(item.delivery_id, item.code) for item in result.failures] == [
        (sent.id, "NOTIFICATION_DELIVERY_NOT_RETRYABLE"),
        (UUID(int=0), "NOTIFICATION_RESOURCE_NOT_FOUND"),
    ]
    assert event.status == NotificationEventStatus.DISPATCHED


def test_event_aggregation_only_uses_latest_generation_per_channel() -> None:
    event = make_event()
    deliveries = [
        make_delivery(event, status=NotificationDeliveryStatus.FAILED),
        make_delivery(
            event,
            status=NotificationDeliveryStatus.SENT,
            generation=2,
        ),
        make_delivery(
            event,
            status=NotificationDeliveryStatus.SENT,
            channel=DeliveryChannel.EMAIL,
        ),
    ]

    assert aggregate_current_event_status(deliveries) == (
        NotificationEventStatus.DELIVERED
    )
