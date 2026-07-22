from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AlertActionType(StrEnum):
    OPENED = "OPENED"
    UPDATED = "UPDATED"
    ESCALATED = "ESCALATED"
    REOPENED = "REOPENED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    AUTO_RESOLVED = "AUTO_RESOLVED"
    RETRY_REQUESTED = "RETRY_REQUESTED"


@dataclass(frozen=True, slots=True)
class ReportAlert:
    aggregation_key: str
    source_event_id: str
    alert_type: str
    object_type: str
    object_id: str
    severity: AlertSeverity
    title: str
    summary: str
    details: dict[str, Any]
    request_id: str
    retry_job_type: str | None = None
    retry_queue: str | None = None
    retry_config: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AlertCommand:
    alert_id: UUID
    expected_version: int
    reason: str
    request_id: str
    idempotency_key: str
    actor_user_id: str
    session_id: str
    trusted_ip: str
