import importlib
import importlib.util
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from long_invest.modules.notifications.channels import (
    ChannelResult,
    ChannelSendRequest,
)
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.eligibility import EligibilityDecision
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
)
from long_invest.modules.notifications.templates import StrictTemplateRenderer


def load_worker():
    module_name = "long_invest.modules.notifications.worker"
    assert importlib.util.find_spec(module_name) is not None, (
        "independent notification worker interface is not implemented"
    )
    return importlib.import_module(module_name)


def claimed_test_delivery():
    event_id = uuid4()
    lease_token = uuid4()
    event = NotificationEvent(
        id=event_id,
        event_type="notification.test",
        business_event_type="notification.test_requested",
        business_event_id="test-1",
        business_object_type="notification_channel",
        business_object_id="WECOM",
        template_variables={"message": "channel check", "event_id": str(event_id)},
        status=NotificationEventStatus.DISPATCHED,
        eligibility_status=NotificationEventStatus.ELIGIBLE,
        effective_channels=[DeliveryChannel.WECOM],
        template_version="v1",
        idempotency_key=f"event-{event_id}",
        content_hash="a" * 64,
        request_id="req-worker-1",
    )
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
        deterministic_message_id=f"notification:{event_id}:WECOM:1",
        lease_owner="notify-wecom-1",
        lease_token=lease_token,
        lease_expires_at=datetime(2026, 7, 15, 1, 1, tzinfo=UTC),
    )
    return event, delivery, lease_token


class FakeWeComChannel:
    channel = DeliveryChannel.WECOM

    def __init__(self, result: ChannelResult) -> None:
        self.result = result
        self.requests: list[ChannelSendRequest] = []

    def validate_config(self, config):
        return ()

    def render(self, template, variables):
        return StrictTemplateRenderer().render(template, variables)

    async def send(self, request):
        self.requests.append(request)
        return self.result

    async def test(self, request):
        return await self.send(request)


@pytest.mark.anyio
async def test_worker_returns_its_channel_result_for_persistence() -> None:
    worker = load_worker()
    event, delivery, lease_token = claimed_test_delivery()
    channel = FakeWeComChannel(ChannelResult.success(summary="accepted"))

    async def eligible(_event, _delivery):
        return EligibilityDecision(True, None, None)

    executor = worker.NotificationWorker(channel=channel, eligibility_reviewer=eligible)

    execution = await executor.execute_claimed(
        worker.ClaimedNotificationDelivery(delivery, lease_token),
        event,
        started_at=datetime(2026, 7, 15, 1, 0, tzinfo=UTC),
    )

    assert execution.result is not None
    assert execution.result.code == "OK"
    assert execution.skip_decision is None
    assert execution.lease.delivery_id == delivery.id
    assert len(channel.requests) == 1
    assert channel.requests[0].event_id == str(event.id)
    assert channel.requests[0].deterministic_message_id == (
        delivery.deterministic_message_id
    )


@pytest.mark.anyio
async def test_worker_stops_before_channel_when_pre_send_eligibility_fails() -> None:
    worker = load_worker()
    event, delivery, lease_token = claimed_test_delivery()
    channel = FakeWeComChannel(ChannelResult.success(summary="must not send"))

    async def ineligible(_event, _delivery):
        return EligibilityDecision(
            False,
            "NOT_HOLDING",
            NotificationDeliveryStatus.SKIPPED_INELIGIBLE,
        )

    executor = worker.NotificationWorker(
        channel=channel,
        eligibility_reviewer=ineligible,
    )

    execution = await executor.execute_claimed(
        worker.ClaimedNotificationDelivery(delivery, lease_token),
        event,
        started_at=datetime(2026, 7, 15, 1, 0, tzinfo=UTC),
    )

    assert execution.result is None
    assert execution.skip_decision is not None
    assert execution.skip_decision.reason == "NOT_HOLDING"
    assert channel.requests == []


@pytest.mark.anyio
async def test_worker_rejects_delivery_from_another_channel() -> None:
    worker = load_worker()
    event, delivery, lease_token = claimed_test_delivery()
    delivery.channel = DeliveryChannel.EMAIL
    channel = FakeWeComChannel(ChannelResult.success(summary="must not send"))

    async def eligible(_event, _delivery):
        return EligibilityDecision(True, None, None)

    executor = worker.NotificationWorker(channel=channel, eligibility_reviewer=eligible)

    execution = await executor.execute_claimed(
        worker.ClaimedNotificationDelivery(delivery, lease_token),
        event,
        started_at=datetime(2026, 7, 15, 1, 0, tzinfo=UTC),
    )

    assert execution.result is not None
    assert execution.result.outcome == "PERMANENT_FAILURE"
    assert execution.result.code == "DELIVERY_CHANNEL_MISMATCH"
    assert channel.requests == []
