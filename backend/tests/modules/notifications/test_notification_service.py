import importlib
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

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
        self.operations: list[str] = []
        self.concurrent_conflict: str | None = None
        self.conflict_raised = False
        self.active_template_version = "v1"
        self.circuit = SimpleNamespace(
            state="CLOSED",
            consecutive_failures=0,
            cooldown_level=0,
            retry_at=None,
            probe_token=None,
        )
        self.resource_events = type(
            "ResourceEvents",
            (),
            {
                "event_changed": AsyncMock(),
                "delivery_changed": AsyncMock(),
            },
        )()

    async def find_event_by_idempotency(self, idempotency_key):
        self.operations.append("find_event_by_idempotency")
        if self.concurrent_conflict is not None and not self.conflict_raised:
            return None
        if self.event.idempotency_key == idempotency_key:
            return self.event
        return None

    async def resolve_active_template(self, template_type, registry):
        if self.active_template_version is None:
            return None
        return registry.resolve(template_type, self.active_template_version)

    async def lock_channel_circuit(self, _channel, _instance):
        return self.circuit

    async def read_channel_circuit(self, _channel, _instance):
        return self.circuit

    async def defer_channel_deliveries(self, **_kwargs):
        return None

    async def release_channel_deliveries(self, **_kwargs):
        return None

    def add_event(self, event):
        self.event = event

    def add_deliveries(self, deliveries):
        self.deliveries.extend(deliveries)

    async def persist_event_and_deliveries(self, event, deliveries):
        self.operations.append("persist_event_and_deliveries")
        if self.concurrent_conflict is not None:
            self.conflict_raised = True
            self.event.idempotency_key = event.idempotency_key
            self.event.content_hash = (
                event.content_hash
                if self.concurrent_conflict == "same"
                else "different-content-hash"
            )
            raise IntegrityError("INSERT", {}, RuntimeError("duplicate key"))
        self.event = event
        self.deliveries.extend(deliveries)

    async def lock_delivery(self, delivery_id):
        return next((item for item in self.deliveries if item.id == delivery_id), None)

    async def get_event(self, event_id):
        return self.event if self.event.id == event_id else None

    async def lock_event(self, event_id):
        self.operations.append("lock_event")
        return self.event if self.event.id == event_id else None

    async def list_deliveries(self, event_id):
        self.operations.append("list_deliveries")
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
    ("result", "expected_status", "expected_event_status", "expected_change"),
    [
        (
            ChannelResult.success(summary="accepted"),
            NotificationDeliveryStatus.SENT,
            NotificationEventStatus.DELIVERED,
            "succeeded",
        ),
        (
            ChannelResult.temporary_failure(
                code="CHANNEL_TIMEOUT",
                summary="timed out",
            ),
            NotificationDeliveryStatus.RETRY_WAIT,
            NotificationEventStatus.DISPATCHED,
            "retry",
        ),
        (
            ChannelResult.permanent_failure(
                code="CHANNEL_AUTH_FAILED",
                summary="rejected",
            ),
            NotificationDeliveryStatus.FAILED,
            NotificationEventStatus.FAILED,
            "failed",
        ),
        (
            ChannelResult.outcome_unknown(
                code="CHANNEL_RESPONSE_LOST",
                summary="response unavailable",
            ),
            NotificationDeliveryStatus.OUTCOME_UNKNOWN,
            NotificationEventStatus.DISPATCHED,
            "outcome_unknown",
        ),
    ],
)
async def test_record_result_writes_attempt_and_advances_delivery_and_event(
    result,
    expected_status,
    expected_event_status,
    expected_change,
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
    assert repository.operations.index("lock_event") < repository.operations.index(
        "list_deliveries"
    )
    repository.resource_events.delivery_changed.assert_awaited_once_with(
        delivery,
        request_id=event.request_id,
        change=expected_change,
        dedupe_token="generation-1-attempt-1",
    )


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
    repository.resource_events.delivery_changed.assert_not_awaited()


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
    repository.resource_events.delivery_changed.assert_awaited_once_with(
        delivery,
        request_id=event.request_id,
        change="skipped_ineligible",
        dedupe_token="generation-1-attempt-1",
    )


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
async def test_cross_channel_recovery_locks_events_in_same_global_order() -> None:
    service = load_service()
    event_ids = (UUID(int=1), UUID(int=2))

    async def recovery_lock_order(channel, returned_event_ids):
        events = []
        deliveries = []
        for event_id in event_ids:
            event, delivery, _lease_token = event_and_delivery()
            event.id = event_id
            delivery.event_id = event_id
            delivery.channel = channel
            events.append(event)
            deliveries.append(delivery)

        class OrderingRepository(FakeRepository):
            def __init__(self):
                super().__init__(events[0], deliveries)
                self.events = {item.id: item for item in events}
                self.expired = [
                    next(
                        item
                        for item in deliveries
                        if item.event_id == returned_event_id
                    )
                    for returned_event_id in returned_event_ids
                ]
                self.locked_event_ids = []

            async def lock_event(self, event_id):
                self.locked_event_ids.append(event_id)
                return self.events[event_id]

        repository = OrderingRepository()
        await service.NotificationService(repository).recover_expired_leases(
            channel=channel,
            now=datetime(2026, 7, 15, 1, 2, tzinfo=UTC),
            limit=10,
        )
        return repository.locked_event_ids

    wecom_order = await recovery_lock_order(
        DeliveryChannel.WECOM,
        tuple(reversed(event_ids)),
    )
    email_order = await recovery_lock_order(
        DeliveryChannel.EMAIL,
        event_ids,
    )

    assert wecom_order == list(event_ids)
    assert email_order == list(event_ids)


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
async def test_publish_uses_active_template_instead_of_caller_version() -> None:
    service = load_service()
    placeholder, _, _ = event_and_delivery()
    placeholder.idempotency_key = "other-event"
    repository = FakeRepository(placeholder, [])
    command = publish_command(service)
    command = replace(command, template_version="missing")

    event = await service.NotificationService(repository).publish(command)

    assert event.template_version == "v1"
    assert len(repository.deliveries) == 1


@pytest.mark.anyio
async def test_publish_rejects_event_type_without_active_template() -> None:
    service = load_service()
    placeholder, _, _ = event_and_delivery()
    placeholder.idempotency_key = "other-event"
    repository = FakeRepository(placeholder, [])
    repository.active_template_version = None

    with pytest.raises(service.NotificationPublishError) as exc_info:
        await service.NotificationService(repository).publish(publish_command(service))

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


@pytest.mark.anyio
async def test_concurrent_same_publish_returns_winner_after_conflict() -> None:
    service = load_service()
    winner, _, _ = event_and_delivery()
    winner.idempotency_key = "unrelated-before-race"
    repository = FakeRepository(winner, [])
    repository.concurrent_conflict = "same"

    returned = await service.NotificationService(repository).publish(
        publish_command(service)
    )

    assert returned is winner
    assert repository.operations == [
        "find_event_by_idempotency",
        "persist_event_and_deliveries",
        "find_event_by_idempotency",
    ]


@pytest.mark.anyio
async def test_concurrent_different_publish_returns_stable_key_reused_error() -> None:
    service = load_service()
    winner, _, _ = event_and_delivery()
    winner.idempotency_key = "unrelated-before-race"
    repository = FakeRepository(winner, [])
    repository.concurrent_conflict = "different"

    with pytest.raises(service.NotificationPublishError) as exc_info:
        await service.NotificationService(repository).publish(publish_command(service))

    assert exc_info.value.code == "NOTIFICATION_IDEMPOTENCY_KEY_REUSED"
    assert not isinstance(exc_info.value, IntegrityError)
