from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from long_invest.modules.notifications.channels import ChannelResult
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    DeliveryOutcome,
    NotificationDeliveryStatus,
)
from long_invest.modules.notifications.delivery import (
    CircuitSnapshot,
    CircuitState,
    enter_half_open,
    record_circuit_failure,
    record_circuit_success,
)
from long_invest.modules.notifications.repository import NotificationRepository

_CIRCUIT_EXCLUDED_CODES = {
    "DELIVERY_CHANNEL_MISMATCH",
    "NOTIFICATION_CHANNEL_CONFIG_INVALID",
    "NOTIFICATION_CHANNEL_DISABLED",
    "NOTIFICATION_TEMPLATE_INVALID",
}


@dataclass(frozen=True, slots=True)
class CircuitPermission:
    allowed: bool
    snapshot: CircuitSnapshot
    probe_token: UUID | None = None


@dataclass(frozen=True, slots=True)
class NewDeliveryState:
    status: NotificationDeliveryStatus
    next_retry_at: datetime | None
    circuit_deferred_until: datetime | None


class NotificationCircuitService:
    def __init__(self, repository: NotificationRepository) -> None:
        self._repository = repository

    async def prepare_delivery(
        self,
        channel: DeliveryChannel,
        *,
        now: datetime,
        instance: str = "primary",
    ) -> CircuitPermission:
        row = await self._repository.lock_channel_circuit(channel, instance)
        snapshot = _snapshot(row)
        if snapshot.state is CircuitState.CLOSED:
            return CircuitPermission(True, snapshot)
        await self._repository.defer_channel_deliveries(
            channel=channel,
            until=_defer_until(snapshot, now),
        )
        return CircuitPermission(False, snapshot)

    async def new_delivery_state(
        self,
        channel: DeliveryChannel,
        *,
        now: datetime,
        instance: str = "primary",
    ) -> NewDeliveryState:
        row = await self._repository.read_channel_circuit(channel, instance)
        snapshot = _snapshot(row) if row is not None else CircuitSnapshot.closed()
        if snapshot.state is CircuitState.DISABLED:
            return NewDeliveryState(
                NotificationDeliveryStatus.SKIPPED_DISABLED, None, None
            )
        if snapshot.state in {CircuitState.OPEN, CircuitState.HALF_OPEN}:
            until = _defer_until(snapshot, now)
            return NewDeliveryState(
                NotificationDeliveryStatus.RETRY_WAIT,
                until,
                until,
            )
        return NewDeliveryState(NotificationDeliveryStatus.PENDING, None, None)

    async def grant_probe(
        self,
        channel: DeliveryChannel,
        *,
        now: datetime,
        instance: str = "primary",
    ) -> CircuitPermission:
        row = await self._repository.lock_channel_circuit(channel, instance)
        snapshot = _snapshot(row)
        half_open = enter_half_open(snapshot, now=now)
        if half_open.state is not CircuitState.HALF_OPEN:
            return CircuitPermission(False, snapshot)
        token = uuid4()
        _apply_snapshot(row, half_open)
        row.probe_token = token
        await self._repository.flush()
        return CircuitPermission(True, half_open, token)

    async def record_result(
        self,
        channel: DeliveryChannel,
        result: ChannelResult,
        *,
        now: datetime,
        instance: str = "primary",
        probe_token: UUID | None = None,
    ) -> CircuitSnapshot:
        row = await self._repository.lock_channel_circuit(channel, instance)
        snapshot = _snapshot(row)
        if snapshot.state is CircuitState.HALF_OPEN and row.probe_token != probe_token:
            return snapshot
        if result.outcome is DeliveryOutcome.SUCCESS:
            updated = record_circuit_success(snapshot)
        elif probe_token is not None or _affects_circuit(result):
            updated = record_circuit_failure(snapshot, now=now)
        else:
            if snapshot.state is CircuitState.HALF_OPEN:
                row.probe_token = None
            return snapshot
        _apply_snapshot(row, updated)
        row.probe_token = None
        if updated.state is CircuitState.OPEN:
            assert updated.retry_at is not None
            await self._repository.defer_channel_deliveries(
                channel=channel,
                until=updated.retry_at,
            )
        elif updated.state is CircuitState.CLOSED:
            await self._repository.release_channel_deliveries(channel=channel)
        await self._repository.flush()
        return updated

    async def reset(
        self,
        channel: DeliveryChannel,
        *,
        instance: str = "primary",
    ) -> CircuitSnapshot:
        row = await self._repository.lock_channel_circuit(channel, instance)
        updated = CircuitSnapshot.closed()
        _apply_snapshot(row, updated)
        row.probe_token = None
        await self._repository.release_channel_deliveries(channel=channel)
        await self._repository.flush()
        return updated


def _snapshot(row) -> CircuitSnapshot:
    return CircuitSnapshot(
        state=CircuitState(row.state),
        consecutive_failures=row.consecutive_failures,
        cooldown_level=row.cooldown_level,
        retry_at=row.retry_at,
    )


def _apply_snapshot(row, snapshot: CircuitSnapshot) -> None:
    row.state = snapshot.state
    row.consecutive_failures = snapshot.consecutive_failures
    row.cooldown_level = snapshot.cooldown_level
    row.retry_at = snapshot.retry_at


def _defer_until(snapshot: CircuitSnapshot, now: datetime) -> datetime:
    if snapshot.retry_at is not None and snapshot.retry_at > now:
        return snapshot.retry_at
    return now + timedelta(seconds=60)


def _affects_circuit(result: ChannelResult) -> bool:
    return (
        result.outcome is not DeliveryOutcome.SUCCESS
        and result.code not in _CIRCUIT_EXCLUDED_CODES
    )
