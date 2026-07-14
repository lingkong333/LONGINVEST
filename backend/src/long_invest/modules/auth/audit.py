import hashlib
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AuditContext:
    request_id: str
    idempotency_key: str
    actor_user_id: str | None = None
    session_id: str | None = None
    trusted_ip: str | None = None


@dataclass(frozen=True, slots=True)
class AuthAuditEvent:
    action_code: str
    object_type: str
    object_id: str
    result: str
    request_id: str
    idempotency_key: str
    risk_level: str
    reason: str | None = None
    before_summary: dict[str, Any] | None = None
    after_summary: dict[str, Any] | None = None
    actor_user_id: str | None = None
    session_id: str | None = None
    trusted_ip: str | None = None


class AuthAuditPort(Protocol):
    async def record(self, event: AuthAuditEvent) -> None: ...


def build_auth_audit_event(
    context: AuditContext,
    *,
    action_code: str,
    object_type: str,
    object_id: str,
    result: str,
    risk_level: str,
    reason: str | None = None,
    before_summary: dict[str, Any] | None = None,
    after_summary: dict[str, Any] | None = None,
    actor_user_id: str | None = None,
    session_id: str | None = None,
) -> AuthAuditEvent:
    idempotency_material = (
        f"{context.idempotency_key}\0{action_code}".encode()
    )
    return AuthAuditEvent(
        action_code=action_code,
        object_type=object_type,
        object_id=object_id,
        result=result,
        request_id=context.request_id,
        idempotency_key=f"auth:{hashlib.sha256(idempotency_material).hexdigest()}",
        risk_level=risk_level,
        reason=reason,
        before_summary=before_summary,
        after_summary=after_summary,
        actor_user_id=actor_user_id or context.actor_user_id,
        session_id=session_id or context.session_id,
        trusted_ip=context.trusted_ip,
    )
