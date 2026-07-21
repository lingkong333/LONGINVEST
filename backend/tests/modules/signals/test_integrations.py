from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.signals.contracts import SignalNotificationPort
from long_invest.modules.signals.integrations import (
    TransactionalNotificationPublisher,
    TransactionalPositionPort,
    TransactionalSubscriptionPort,
)


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
        lambda received_session: PublicPort()
        if received_session is session
        else None,
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
async def test_notification_adapter_uses_public_factory_without_committing(
    monkeypatch,
) -> None:
    from long_invest.modules.signals import integrations

    command = object()
    expected = object()

    class Session:
        async def commit(self):
            raise AssertionError("caller owns commit")

    session = Session()

    class Service:
        async def publish(self, received):
            assert received is command
            return expected

    monkeypatch.setattr(
        integrations,
        "transactional_notification_service",
        lambda received_session: Service()
        if received_session is session
        else SimpleNamespace(),
    )

    result = await TransactionalNotificationPublisher(session).publish(command)

    assert result is expected
