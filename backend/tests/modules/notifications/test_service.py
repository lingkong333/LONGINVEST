import importlib
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from long_invest.modules.notifications.channels import ChannelResult
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationEvent,
)


def load_service():
    module_name = "long_invest.modules.notifications.service"
    assert importlib.util.find_spec(module_name) is not None, (
        "notification application service is not implemented"
    )
    return importlib.import_module(module_name)


class FakeRepository:
    def __init__(
        self,
        event: NotificationEvent,
        deliveries: list[NotificationDelivery],
    ) -> None:
        self.event = event
        self.deliveries = deliveries
        self.attempts: list[NotificationDeliveryAttempt] = []
        self.expired: list[NotificationDelivery] = []
        self.flush_count = 0

    async def find_event_by_idempotency(self, idempotency_key):
        if self.event.idempotency_key == idempotency_key:
            return self.event
        return None

    def add_event(self, event):
        self.event = event

    def add_deliveries(self, deliveries):
        self.deliveries.extend(deliveries)

    async def lock_delivery(self, delivery_id):
        return next((item for item in self.deliveries if item.id == delivery_id), None)

    async def get_event(self, event_id):
        return self.event if self.event.id == event_id else None

    async def list_deliveries(self, event_id):
        return [item for item in self.deliveries if item.event_id == event_id]

    def add_attempt(self, attempt):
        self.attempts.append(attempt)

    async def lock_expired_leases(self, *, channel, now, limit):
        return list(self.expired[:limit])

    async def flush(self):
        self.flush_count += 1


def event_and_delivery():
    event_id = uuid4()
    event = NotificationEvent(
        id=event_id,
        event_type="signal.high",
        business_event_type="signal.transitioned",
        business_event_id="signal-1",
        business_object_type="subscription",
        business_object_id="subscription-1",
        template_variables={"symbol": "600000.SH"},
        status=NotificationEventStatus.DISPATCHED,
        eligibility_status=NotificationEventStatus.ELIGIBLE,
        effective_channels=[DeliveryChannel.WECOM],
        template_version="v1",
        idempotency_key=f"event-{uuid4()}",
        request_id="req-notify-1",
    )
    lease_token = uuid4()
    delivery = NotificationDelivery(
        id=uuid4(),
        event_id=event_id,
        generation=1,
        channel=DeliveryChannel.WECOM,
        config_version=1,
        target_fingerprint="wecom:primary",
        status=NotificationDeliveryStatus.SENDING,
        attempt_count=0,
        unknown_compensation_count=0,
        deterministic_message_id=f"message-{uuid4()}",
        lease_owner="notify-wecom-1",
        lease_token=lease_token,
        lease_expires_at=datetime(2026, 7, 15, 1, 1, tzinfo=UTC),
    )
    return event, delivery, lease_token


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "expected_status", "expected_event_status"),
    [
        (
            ChannelResult.success(summary="accepted"),
            NotificationDeliveryStatus.SENT,
            NotificationEventStatus.DELIVERED,
        ),
        (
            ChannelResult.temporary_failure(
                code="CHANNEL_TIMEOUT",
                summary="timed out",
            ),
            NotificationDeliveryStatus.RETRY_WAIT,
            NotificationEventStatus.DISPATCHED,
        ),
        (
            ChannelResult.permanent_failure(
                code="CHANNEL_AUTH_FAILED",
                summary="rejected",
            ),
            NotificationDeliveryStatus.FAILED,
            NotificationEventStatus.FAILED,
        ),
        (
            ChannelResult.outcome_unknown(
                code="CHANNEL_RESPONSE_LOST",
                summary="response unavailable",
            ),
            NotificationDeliveryStatus.OUTCOME_UNKNOWN,
            NotificationEventStatus.DISPATCHED,
        ),
    ],
)
async def test_record_result_writes_attempt_and_advances_delivery_and_event(
    result,
    expected_status,
    expected_event_status,
) -> None:
    service = load_service()
    event, delivery, lease_token = event_and_delivery()
    repository = FakeRepository(event, [delivery])
    now = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)

    accepted = await service.NotificationService(repository).record_result(
        service.DeliveryLease(delivery.id, lease_token),
        result=result,
        started_at=now,
        finished_at=now + timedelta(milliseconds=125),
    )

    assert accepted is True
    assert delivery.status == expected_status
    assert event.status == expected_event_status
    assert delivery.attempt_count == 1
    assert delivery.lease_owner is None
    assert delivery.lease_token is None
    assert delivery.lease_expires_at is None
    assert len(repository.attempts) == 1
    assert repository.attempts[0].attempt_no == 1
    assert repository.attempts[0].outcome == result.outcome
    assert repository.attempts[0].duration_ms == 125


@pytest.mark.anyio
async def test_stale_worker_lease_cannot_overwrite_reclaimed_delivery() -> None:
    service = load_service()
    event, delivery, old_token = event_and_delivery()
    delivery.lease_token = uuid4()
    repository = FakeRepository(event, [delivery])
    now = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)

    accepted = await service.NotificationService(repository).record_result(
        service.DeliveryLease(delivery.id, old_token),
        result=ChannelResult.success(summary="late result"),
        started_at=now,
        finished_at=now,
    )

    assert accepted is False
    assert delivery.status == NotificationDeliveryStatus.SENDING
    assert repository.attempts == []


@pytest.mark.anyio
async def test_skip_delivery_is_fenced_and_advances_event_without_attempt() -> None:
    service = load_service()
    event, delivery, lease_token = event_and_delivery()
    repository = FakeRepository(event, [delivery])
    now = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)

    accepted = await service.NotificationService(repository).skip_delivery(
        service.DeliveryLease(delivery.id, lease_token),
        delivery_status=NotificationDeliveryStatus.SKIPPED_INELIGIBLE,
        reason="NOT_HOLDING",
        now=now,
    )

    assert accepted is True
    assert delivery.status == NotificationDeliveryStatus.SKIPPED_INELIGIBLE
    assert delivery.error_code == "NOT_HOLDING"
    assert delivery.lease_token is None
    assert event.status == NotificationEventStatus.SUPPRESSED
    assert repository.attempts == []


@pytest.mark.anyio
async def test_expired_sending_lease_becomes_unknown_and_gets_one_compensation() -> (
    None
):
    service = load_service()
    event, delivery, _lease_token = event_and_delivery()
    repository = FakeRepository(event, [delivery])
    repository.expired = [delivery]
    now = datetime(2026, 7, 15, 1, 2, tzinfo=UTC)

    recovered = await service.NotificationService(repository).recover_expired_leases(
        channel=DeliveryChannel.WECOM,
        now=now,
        limit=10,
    )

    assert recovered == 1
    assert delivery.status == NotificationDeliveryStatus.OUTCOME_UNKNOWN
    assert delivery.unknown_compensation_count == 1
    assert delivery.next_retry_at == now + timedelta(seconds=5)
    assert delivery.lease_owner is None
    assert len(repository.attempts) == 1
    assert repository.attempts[0].outcome == "OUTCOME_UNKNOWN"
    assert repository.attempts[0].possibly_delivered is True


@pytest.mark.anyio
async def test_failed_unknown_compensation_does_not_schedule_another_send() -> None:
    service = load_service()
    event, delivery, lease_token = event_and_delivery()
    delivery.attempt_count = 1
    delivery.unknown_compensation_count = 1
    repository = FakeRepository(event, [delivery])
    now = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)

    accepted = await service.NotificationService(repository).record_result(
        service.DeliveryLease(delivery.id, lease_token),
        result=ChannelResult.temporary_failure(
            code="CHANNEL_TIMEOUT",
            summary="compensation timed out",
        ),
        started_at=now,
        finished_at=now,
    )

    assert accepted is True
    assert delivery.status == NotificationDeliveryStatus.FAILED
    assert delivery.next_retry_at is None


def publish_command(service, *, message="channel check"):
    return service.PublishNotification(
        event_type="notification.test",
        business_event_type="notification.test_requested",
        business_event_id="test-1",
        business_object_type="notification_channel",
        business_object_id="WECOM",
        severity=None,
        template_variables={"message": message},
        template_version="v1",
        targets=(
            service.ChannelDeliveryTarget(
                channel=DeliveryChannel.WECOM,
                config_version=3,
                target_fingerprint="wecom:primary",
            ),
        ),
        idempotency_key="notification-test-1",
        request_id="req-notification-test-1",
    )


@pytest.mark.anyio
async def test_publish_freezes_resolvable_template_and_creates_channel_delivery() -> (
    None
):
    service = load_service()
    placeholder, _, _ = event_and_delivery()
    placeholder.idempotency_key = "other-event"
    repository = FakeRepository(placeholder, [])

    event = await service.NotificationService(repository).publish(
        publish_command(service)
    )

    assert event.event_type == "notification.test"
    assert event.template_version == "v1"
    assert event.template_variables["event_id"] == str(event.id)
    assert event.status == NotificationEventStatus.DISPATCHED
    assert event.content_hash
    assert len(repository.deliveries) == 1
    delivery = repository.deliveries[0]
    assert delivery.event_id == event.id
    assert delivery.channel == DeliveryChannel.WECOM
    assert delivery.status == NotificationDeliveryStatus.PENDING
    assert str(event.id) in delivery.deterministic_message_id


@pytest.mark.anyio
async def test_publish_rejects_unknown_frozen_template_version() -> None:
    service = load_service()
    placeholder, _, _ = event_and_delivery()
    placeholder.idempotency_key = "other-event"
    repository = FakeRepository(placeholder, [])
    command = publish_command(service)
    command = replace(command, template_version="missing")

    with pytest.raises(service.NotificationPublishError) as exc_info:
        await service.NotificationService(repository).publish(command)

    assert exc_info.value.code == "NOTIFICATION_TEMPLATE_VERSION_NOT_FOUND"
    assert repository.deliveries == []


@pytest.mark.anyio
async def test_publish_rejects_idempotency_key_reused_for_different_content() -> None:
    service = load_service()
    placeholder, _, _ = event_and_delivery()
    placeholder.idempotency_key = "notification-test-1"
    placeholder.content_hash = "not-the-new-content-hash"
    repository = FakeRepository(placeholder, [])

    with pytest.raises(service.NotificationPublishError) as exc_info:
        await service.NotificationService(repository).publish(
            publish_command(service, message="different content")
        )

    assert exc_info.value.code == "NOTIFICATION_IDEMPOTENCY_KEY_REUSED"
