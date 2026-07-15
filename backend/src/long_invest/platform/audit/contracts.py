from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AuditWrite:
    action_code: str
    object_type: str
    object_id: str
    result: str
    request_id: str
    idempotency_key: str
    risk_level: str
    reason: str | None
    before_summary: dict[str, Any] | None
    after_summary: dict[str, Any] | None
    actor_user_id: str | None = None
    session_id: str | None = None
    trusted_ip: str | None = None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    action_code: str
    object_type: str
    object_id: str
    result: str
    request_id: str
    idempotency_key: str
    risk_level: str
    reason: str | None
    before_summary: dict[str, Any] | None
    after_summary: dict[str, Any] | None
    actor_user_id: str | None = None
    session_id: str | None = None
    trusted_ip: str | None = None
