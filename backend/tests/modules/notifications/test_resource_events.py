from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
)
from long_invest.modules.notifications.resource_events import (
    NotificationResourceEvents,
)


def make_event() -> NotificationEvent:
    return NotificationEvent(
        id=uuid4(),
        event_type="signal.high",
        business_event_type="signal.transitioned",
        business_event_id="signal-1",
        business_object_type="subscription",
        business_object_id="subscription-1",
        template_variables={"symbol": "600000.SH", "holding": True},
        status=NotificationEventStatus.DISPATCHED,
        eligibility_status=NotificationEventStatus.ELIGIBLE,
        effective_channels=[DeliveryChannel.EMAIL],
        template_version="v1",
        idempotency_key=f"event-{uuid4()}",
        content_hash="a" * 64,
        request_id="request-1",
    )


def make_delivery(event: NotificationEvent) -> NotificationDelivery:
    return NotificationDelivery(
        id=uuid4(),
        event_id=event.id,
        generation=2,
        channel=DeliveryChannel.EMAIL,
        config_version=3,
        target_fingerprint="secret-target-fingerprint",
        status=NotificationDeliveryStatus.RETRY_WAIT,
        attempt_count=1,
        unknown_compensation_count=0,
        error_code="secret-provider-response",
        deterministic_message_id=f"message-{uuid4()}",
    )


@pytest.mark.anyio
async def test_resource_events_are_stable_and_strictly_redacted() -> None:
    session = AsyncMock()
    writer = AsyncMock()
    publisher = NotificationResourceEvents(session, writer)
    event = make_event()
    delivery = make_delivery(event)

    await publisher.event_changed(
        event,
        change="requested",
        dedupe_token="created",
    )
    await publisher.delivery_changed(
        delivery,
        request_id=event.request_id,
        change="retry",
        dedupe_token="generation-2-attempt-1",
    )

    event_call, delivery_call = writer.append.await_args_list
    assert event_call.kwargs == {
        "session": session,
        "topic": "notification.changed.v1",
        "aggregate_type": "notification_event",
        "aggregate_id": str(event.id),
        "queue": "maintenance",
        "payload": {
            "notification_event_id": str(event.id),
            "status": "DISPATCHED",
            "request_id": "request-1",
            "change": "requested",
        },
        "dedupe_key": (f"notification-changed:event:{event.id}:created:requested"),
    }
    assert delivery_call.kwargs == {
        "session": session,
        "topic": "notification.changed.v1",
        "aggregate_type": "notification_delivery",
        "aggregate_id": str(delivery.id),
        "queue": "maintenance",
        "payload": {
            "notification_delivery_id": str(delivery.id),
            "status": "RETRY_WAIT",
            "generation": 2,
            "request_id": "request-1",
            "change": "retry",
        },
        "dedupe_key": (
            f"notification-changed:delivery:{delivery.id}:generation-2-attempt-1:retry"
        ),
    }
