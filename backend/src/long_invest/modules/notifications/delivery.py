from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    DeliveryOutcome,
)
from long_invest.modules.notifications.contracts import (
    NotificationDeliveryStatus as ChannelDeliveryStatus,
)


class DeliveryAction(StrEnum):
    SENT = "SENT"
    RETRY = "RETRY"
    FAIL = "FAIL"
    KEEP_UNKNOWN = "KEEP_UNKNOWN"


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    action: DeliveryAction
    reason: str
    delay_seconds: int | None = None
    consume_unknown_compensation: bool = False


class RetryPolicy:
    retry_delays_seconds = (5, 30, 120, 600, 1800)
    max_requests = 6

    def decide(
        self,
        *,
        outcome: DeliveryOutcome,
        request_count: int,
        unknown_compensation_count: int,
    ) -> DeliveryDecision:
        if not 1 <= request_count <= self.max_requests:
            raise ValueError("request counter is outside the valid range")
        if not 0 <= unknown_compensation_count <= 1:
            raise ValueError("unknown compensation counter is outside the valid range")
        if outcome is DeliveryOutcome.SUCCESS:
            return DeliveryDecision(DeliveryAction.SENT, "DELIVERED")
        if outcome is DeliveryOutcome.PERMANENT_FAILURE:
            return DeliveryDecision(DeliveryAction.FAIL, "PERMANENT_FAILURE")
        if outcome is DeliveryOutcome.OUTCOME_UNKNOWN:
            if unknown_compensation_count >= 1 or request_count >= self.max_requests:
                return DeliveryDecision(
                    DeliveryAction.KEEP_UNKNOWN,
                    "UNKNOWN_COMPENSATION_EXHAUSTED",
                )
            return DeliveryDecision(
                DeliveryAction.RETRY,
                "UNKNOWN_COMPENSATION",
                delay_seconds=self.retry_delays_seconds[request_count - 1],
                consume_unknown_compensation=True,
            )
        if request_count >= self.max_requests:
            return DeliveryDecision(
                DeliveryAction.FAIL,
                "MAX_REQUESTS_EXHAUSTED",
            )
        return DeliveryDecision(
            DeliveryAction.RETRY,
            "TEMPORARY_FAILURE",
            delay_seconds=self.retry_delays_seconds[request_count - 1],
        )


_WORKABLE_STATUSES = {
    ChannelDeliveryStatus.PENDING,
    ChannelDeliveryStatus.RETRY_WAIT,
    ChannelDeliveryStatus.OUTCOME_UNKNOWN,
    ChannelDeliveryStatus.FAILED,
}


def channels_requiring_work(
    statuses: Mapping[DeliveryChannel, ChannelDeliveryStatus],
) -> set[DeliveryChannel]:
    return {
        channel for channel, status in statuses.items() if status in _WORKABLE_STATUSES
    }


def aggregate_event_status(statuses: Iterable[ChannelDeliveryStatus]) -> str:
    values = tuple(statuses)
    if not values:
        return "SUPPRESSED"
    active_statuses = {
        ChannelDeliveryStatus.PENDING,
        ChannelDeliveryStatus.SENDING,
        ChannelDeliveryStatus.RETRY_WAIT,
    }
    if any(status in active_statuses for status in values):
        return "DISPATCHED"
    sent_count = values.count(ChannelDeliveryStatus.SENT)
    if sent_count == len(values):
        return "DELIVERED"
    if sent_count:
        return "PARTIAL"
    if all(status is ChannelDeliveryStatus.CANCELED for status in values):
        return "CANCELED"
    skipped_statuses = {
        ChannelDeliveryStatus.SKIPPED_DISABLED,
        ChannelDeliveryStatus.SKIPPED_INELIGIBLE,
    }
    if all(status in skipped_statuses for status in values):
        return "SUPPRESSED"
    terminal_failures = {
        ChannelDeliveryStatus.FAILED,
        ChannelDeliveryStatus.SKIPPED_DISABLED,
        ChannelDeliveryStatus.SKIPPED_INELIGIBLE,
        ChannelDeliveryStatus.CANCELED,
    }
    if all(status in terminal_failures for status in values):
        return "FAILED"
    return "DISPATCHED"


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class CircuitKey:
    channel: DeliveryChannel
    instance: str

    def __str__(self) -> str:
        return f"{self.channel.value}:{self.instance}"


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    state: CircuitState
    consecutive_failures: int
    cooldown_level: int
    retry_at: datetime | None

    @classmethod
    def closed(cls) -> "CircuitSnapshot":
        return cls(CircuitState.CLOSED, 0, 0, None)


_COOLDOWN_SECONDS = (60, 180, 300)


def record_circuit_failure(
    snapshot: CircuitSnapshot,
    *,
    now: datetime,
) -> CircuitSnapshot:
    if snapshot.state is CircuitState.DISABLED:
        return snapshot
    if snapshot.state is CircuitState.HALF_OPEN:
        cooldown_level = min(snapshot.cooldown_level + 1, 2)
        return CircuitSnapshot(
            CircuitState.OPEN,
            3,
            cooldown_level,
            now + timedelta(seconds=_COOLDOWN_SECONDS[cooldown_level]),
        )
    failure_count = snapshot.consecutive_failures + 1
    if failure_count < 3:
        return CircuitSnapshot(CircuitState.CLOSED, failure_count, 0, None)
    return CircuitSnapshot(
        CircuitState.OPEN,
        failure_count,
        snapshot.cooldown_level,
        now + timedelta(seconds=_COOLDOWN_SECONDS[snapshot.cooldown_level]),
    )


def enter_half_open(
    snapshot: CircuitSnapshot,
    *,
    now: datetime,
) -> CircuitSnapshot:
    if snapshot.state is not CircuitState.OPEN:
        return snapshot
    if snapshot.retry_at is None or now < snapshot.retry_at:
        return snapshot
    return CircuitSnapshot(
        CircuitState.HALF_OPEN,
        snapshot.consecutive_failures,
        snapshot.cooldown_level,
        None,
    )


def record_circuit_success(snapshot: CircuitSnapshot) -> CircuitSnapshot:
    if snapshot.state in {CircuitState.CLOSED, CircuitState.HALF_OPEN}:
        return CircuitSnapshot.closed()
    return snapshot


def circuit_allows_request(
    snapshot: CircuitSnapshot,
    *,
    now: datetime,
) -> bool:
    del now
    return snapshot.state in {CircuitState.CLOSED, CircuitState.HALF_OPEN}
