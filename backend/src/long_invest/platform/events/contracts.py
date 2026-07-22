from dataclasses import dataclass
from typing import Protocol

TOPIC_RESOURCE_TYPES: dict[str, str] = {
    "jobs.dispatch": "jobs",
    "jobs.control": "jobs",
    "job.changed.v1": "jobs",
    "notification.changed.v1": "notifications",
    "quote_cycle.created": "quote_cycles",
    "quote_cycle.finalized": "quote_cycles",
    "quote_conflict.detected": "quote_cycles",
    "quote_item.missing": "quote_cycles",
    "signal.state_reset": "signals",
    "signal.evaluation_requested": "signals",
    "signal.evaluation_skipped": "signals",
    "signal.evaluation_completed": "signals",
    "signal.transitioned": "signals",
    "signal.notification_requested": "notifications",
    "signal.notification_suppressed": "signals",
    "notification.admin.changed.v1": "notifications",
    "provider.config_changed": "providers",
    "provider.circuit_probed": "providers",
    "provider.circuit_reset_probed": "providers",
    "provider.quote_diagnostics": "providers",
    "provider.degraded": "providers",
    "provider.auto_switched": "providers",
    "provider.schema_changed": "providers",
    "provider.circuit_state_changed": "providers",
    "provider.circuit_opened": "providers",
    "provider.half_opened": "providers",
    "provider.recovered": "providers",
    "provider.rate_limited": "providers",
    "provider.request_succeeded": "providers",
    "provider.request_failed": "providers",
    "alert.opened.v1": "alerts",
    "alert.updated.v1": "alerts",
    "alert.escalated.v1": "alerts",
    "alert.reopened.v1": "alerts",
    "alert.acknowledged.v1": "alerts",
    "alert.resolved.v1": "alerts",
    "alert.auto_resolved.v1": "alerts",
    "alert.retry_requested.v1": "alerts",
    "settings.changed.v1": "settings",
    "secrets.changed.v1": "settings",
}
SUPPORTED_TOPICS = tuple(TOPIC_RESOURCE_TYPES)


@dataclass(frozen=True, slots=True)
class StoredResourceEvent:
    sequence: int
    topic: str
    aggregate_id: str


class EventSource(Protocol):
    async def latest_sequence(self) -> int: ...

    async def contains_sequence(self, sequence: int) -> bool: ...

    async def fetch_after(
        self, sequence: int, *, limit: int
    ) -> tuple[StoredResourceEvent, ...]: ...
