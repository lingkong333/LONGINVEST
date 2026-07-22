from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from long_invest.modules.notifications.channels import ChannelResult
from long_invest.modules.notifications.circuit import NotificationCircuitService
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
)
from long_invest.modules.notifications.delivery import CircuitState


class Repository:
    def __init__(self) -> None:
        self.row = SimpleNamespace(
            state=CircuitState.CLOSED,
            consecutive_failures=0,
            cooldown_level=0,
            retry_at=None,
            probe_token=None,
        )
        self.deferred_until = None
        self.release_count = 0
        self.flush_count = 0

    async def lock_channel_circuit(self, _channel, _instance):
        return self.row

    async def read_channel_circuit(self, _channel, _instance):
        return self.row

    async def defer_channel_deliveries(self, *, channel, until):
        assert channel is DeliveryChannel.WECOM
        self.deferred_until = until

    async def release_channel_deliveries(self, *, channel):
        assert channel is DeliveryChannel.WECOM
        self.release_count += 1

    async def flush(self):
        self.flush_count += 1


@pytest.mark.anyio
async def test_new_delivery_does_not_create_or_lock_a_missing_circuit() -> None:
    repository = Repository()
    repository.row = None

    state = await NotificationCircuitService(repository).new_delivery_state(
        DeliveryChannel.WECOM,
        now=datetime(2026, 7, 22, tzinfo=UTC),
    )

    assert state.status is NotificationDeliveryStatus.PENDING


@pytest.mark.anyio
async def test_three_failures_open_circuit_and_defer_new_deliveries() -> None:
    repository = Repository()
    service = NotificationCircuitService(repository)
    now = datetime(2026, 7, 22, tzinfo=UTC)
    failure = ChannelResult.temporary_failure(code="TIMEOUT", summary="timeout")

    for offset in range(3):
        result = await service.record_result(
            DeliveryChannel.WECOM,
            failure,
            now=now + timedelta(seconds=offset),
        )

    assert result.state is CircuitState.OPEN
    assert result.retry_at == now + timedelta(seconds=62)
    assert repository.deferred_until == result.retry_at
    state = await service.new_delivery_state(
        DeliveryChannel.WECOM, now=now + timedelta(seconds=3)
    )
    assert state.status is NotificationDeliveryStatus.RETRY_WAIT
    assert state.next_retry_at == result.retry_at


@pytest.mark.anyio
async def test_probe_only_runs_after_cooldown_and_success_recovers_channel() -> None:
    repository = Repository()
    service = NotificationCircuitService(repository)
    now = datetime(2026, 7, 22, tzinfo=UTC)
    repository.row.state = CircuitState.OPEN
    repository.row.consecutive_failures = 3
    repository.row.retry_at = now + timedelta(seconds=60)

    denied = await service.grant_probe(DeliveryChannel.WECOM, now=now)
    granted = await service.grant_probe(
        DeliveryChannel.WECOM, now=now + timedelta(seconds=60)
    )
    recovered = await service.record_result(
        DeliveryChannel.WECOM,
        ChannelResult.success(summary="ok"),
        now=now + timedelta(seconds=61),
        probe_token=granted.probe_token,
    )

    assert denied.allowed is False
    assert granted.allowed is True
    assert recovered.state is CircuitState.CLOSED
    assert repository.release_count == 1


@pytest.mark.anyio
async def test_manual_reset_closes_open_circuit() -> None:
    repository = Repository()
    repository.row.state = CircuitState.OPEN
    repository.row.consecutive_failures = 4
    repository.row.cooldown_level = 1
    repository.row.retry_at = datetime(2026, 7, 22, tzinfo=UTC)

    result = await NotificationCircuitService(repository).reset(DeliveryChannel.WECOM)

    assert result.state is CircuitState.CLOSED
    assert repository.row.retry_at is None
    assert repository.release_count == 1
