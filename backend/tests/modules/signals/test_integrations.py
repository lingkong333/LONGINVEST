from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationEventStatus,
)
from long_invest.modules.notifications.service import (
    ChannelDeliveryTarget,
    PublishNotification,
)
from long_invest.modules.positions.contracts import PositionStatus
from long_invest.modules.signals.contracts import (
    EvaluationReason,
    NotificationClass,
    SignalNotificationPort,
    SignalNotificationRequest,
    SignalZone,
)
from long_invest.modules.signals.integrations import (
    SignalNotificationPolicyUnavailable,
    TransactionalNotificationPublisher,
    TransactionalPositionPort,
    TransactionalQuotePort,
    TransactionalSubscriptionPort,
    TransactionalTargetPort,
)
from long_invest.modules.targets.contracts import TargetValues


def test_signal_notification_port_exposes_publish_contract() -> None:
    assert "publish" in SignalNotificationPort.__dict__


@pytest.mark.anyio
async def test_subscription_adapter_calls_public_monitoring_port(monkeypatch) -> None:
    from long_invest.modules.signals import integrations

    subscription_id = uuid4()
    expected = object()
    session = object()

    class PublicPort:
        async def lock(self, received_id):
            assert received_id == subscription_id
            return expected

    monkeypatch.setattr(
        integrations,
        "transactional_monitor_subscription_port",
        lambda received_session: PublicPort() if received_session is session else None,
    )

    assert (
        await TransactionalSubscriptionPort(session).lock(subscription_id) is expected
    )


@pytest.mark.anyio
async def test_position_adapter_calls_public_position_application(monkeypatch) -> None:
    from long_invest.modules.signals import integrations

    security_id = uuid4()
    expected = object()
    session = object()

    async def public_snapshot(received_session, received_id):
        assert (received_session, received_id) == (session, security_id)
        return expected

    monkeypatch.setattr(integrations, "get_position_snapshot", public_snapshot)

    assert (
        await TransactionalPositionPort(session).get_position_snapshot(security_id)
        is expected
    )


@pytest.mark.anyio
async def test_target_adapter_calls_public_target_factory(monkeypatch) -> None:
    from long_invest.modules.signals import integrations

    subscription_id = uuid4()
    expected = object()
    session = object()

    class PublicPort:
        async def get_target_snapshot(self, received_id):
            assert received_id == subscription_id
            return expected

    monkeypatch.setattr(
        integrations,
        "transactional_target_snapshot_port",
        lambda received_session: PublicPort() if received_session is session else None,
    )
    assert (
        await TransactionalTargetPort(session).get_target_snapshot(subscription_id)
        is expected
    )


@pytest.mark.anyio
async def test_quote_adapter_calls_public_quote_factory(monkeypatch) -> None:
    from long_invest.modules.signals import integrations

    item_id = uuid4()
    cycle_id = uuid4()
    expected = object()
    session = object()

    class PublicPort:
        async def get_quote_snapshot(self, **keys):
            assert keys == {"item_id": item_id, "cycle_id": cycle_id}
            return expected

    monkeypatch.setattr(
        integrations,
        "transactional_quote_signal_port",
        lambda received_session: PublicPort() if received_session is session else None,
    )

    assert (
        await TransactionalQuotePort(session).get_quote_snapshot(
            item_id=item_id,
            cycle_id=cycle_id,
        )
        is expected
    )


@pytest.mark.anyio
async def test_notification_adapter_translates_eligible_signal_request(
    monkeypatch,
) -> None:
    from long_invest.modules.signals import integrations

    request = _notification_request()
    expected = object()
    target = ChannelDeliveryTarget(
        channel=DeliveryChannel.EMAIL,
        config_version=3,
        target_fingerprint="email:primary",
    )

    class Session:
        async def commit(self):
            raise AssertionError("caller owns commit")

    session = Session()

    class Service:
        async def publish(self, received):
            assert isinstance(received, PublishNotification)
            assert received is not request
            assert received.event_type == "signal.low"
            assert received.business_event_type == "signal.transitioned"
            assert received.business_event_id == str(request.event_id)
            assert received.business_object_type == "monitor_subscription"
            assert received.business_object_id == str(request.subscription_id)
            assert received.template_version == "v1"
            assert received.targets == (target,)
            assert received.eligibility_status is NotificationEventStatus.ELIGIBLE
            assert received.suppression_reason is None
            assert received.idempotency_key == request.idempotency_key
            assert received.request_id == request.request_id
            assert received.template_variables == {
                "symbol": "600000.SH",
                "name": "Pudong Development Bank",
                "previous_state": "NORMAL",
                "current_state": "LOW",
                "price": "9.000000",
                "quote_time": "2026-07-17T09:30:00+00:00",
                "targets": {
                    "low_strong": "8.00",
                    "low_watch": "9.00",
                    "high_watch": "11.00",
                    "high_strong": "12.00",
                },
                "target_version": 2,
                "target_date": "2026-07-17",
                "target_stale": False,
                "holding": False,
                "reason": "SCHEDULED_QUOTE",
            }
            return expected

    class Resolver:
        async def resolve_targets(self, received):
            assert received is request
            return (target,)

    monkeypatch.setattr(
        integrations,
        "transactional_notification_service",
        lambda received_session: Service() if received_session is session else None,
    )

    result = await TransactionalNotificationPublisher(
        session,
        target_resolver=Resolver(),
    ).publish(request)

    assert result is expected


@pytest.mark.anyio
async def test_notification_adapter_persists_suppressed_fact_without_channels(
    monkeypatch,
) -> None:
    from long_invest.modules.signals import integrations

    request = _notification_request(
        notification_class=NotificationClass.HIGH,
        before_zone=SignalZone.NORMAL,
        after_zone=SignalZone.HIGH,
        eligible=False,
        suppression_reason="NOT_HOLDING",
    )

    class Service:
        async def publish(self, received):
            assert isinstance(received, PublishNotification)
            assert received.event_type == "signal.high"
            assert received.targets == ()
            assert received.eligibility_status is NotificationEventStatus.SUPPRESSED
            assert received.suppression_reason == "NOT_HOLDING"
            return object()

    class Resolver:
        async def resolve_targets(self, received):
            raise AssertionError("suppressed facts do not resolve delivery channels")

    monkeypatch.setattr(
        integrations,
        "transactional_notification_service",
        lambda received_session: Service(),
    )

    await TransactionalNotificationPublisher(
        object(),
        target_resolver=Resolver(),
    ).publish(request)


@pytest.mark.anyio
async def test_eligible_notification_requires_explicit_policy_resolver() -> None:
    publisher = TransactionalNotificationPublisher(object())

    with pytest.raises(SignalNotificationPolicyUnavailable) as exc_info:
        await publisher.publish(_notification_request())

    assert exc_info.value.code == "SIGNAL_NOTIFICATION_POLICY_UNAVAILABLE"


def _notification_request(**overrides) -> SignalNotificationRequest:
    values = {
        "event_id": uuid4(),
        "subscription_id": uuid4(),
        "security_id": uuid4(),
        "symbol": "600000.SH",
        "security_name": "Pudong Development Bank",
        "notification_class": NotificationClass.LOW,
        "before_zone": SignalZone.NORMAL,
        "after_zone": SignalZone.LOW,
        "price": Decimal("9"),
        "price_at": datetime(2026, 7, 17, 9, 30, tzinfo=UTC),
        "targets": TargetValues(
            low_strong="8",
            low_watch="9",
            high_watch="11",
            high_strong="12",
        ),
        "target_revision_id": uuid4(),
        "target_version": 2,
        "target_date": date(2026, 7, 17),
        "target_stale": False,
        "position_status": PositionStatus.NOT_HOLDING,
        "position_version": 4,
        "reason": EvaluationReason.SCHEDULED_QUOTE,
        "notification_mode": "INHERIT",
        "eligible": True,
        "suppression_reason": None,
        "idempotency_key": "signal-event:1",
        "request_id": "request-1",
    }
    values.update(overrides)
    return SignalNotificationRequest(**values)
