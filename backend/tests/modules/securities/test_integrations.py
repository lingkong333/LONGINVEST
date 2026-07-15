from unittest.mock import AsyncMock, Mock

import pytest

from long_invest.modules.securities.contracts import SecurityAuditContext
from long_invest.modules.securities.integrations import (
    SecurityAuditAdapter,
    SecurityMasterAuditEvent,
    SecurityMasterUpdatedEvent,
    TransactionalSecurityEventAdapter,
)


def context() -> SecurityAuditContext:
    return SecurityAuditContext(
        request_id="req_12345678",
        idempotency_key="refresh-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        reason="scheduled security master refresh",
    )


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "idempotency_key",
        "actor_user_id",
        "session_id",
        "trusted_ip",
        "reason",
    ],
)
def test_security_audit_context_requires_every_field(field: str) -> None:
    values = {
        "request_id": "req_12345678",
        "idempotency_key": "refresh-1",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
        "reason": "scheduled security master refresh",
    }
    values[field] = ""

    with pytest.raises(ValueError, match="审计上下文"):
        SecurityAuditContext(**values)


@pytest.mark.anyio
async def test_audit_adapter_uses_public_service_bound_to_the_same_session(
    monkeypatch,
) -> None:
    session = Mock()
    audit_service = Mock()
    audit_service.append = AsyncMock()
    service_factory = Mock(return_value=audit_service)
    monkeypatch.setattr(
        "long_invest.modules.securities.integrations.AuditService",
        service_factory,
    )
    adapter = SecurityAuditAdapter(session)

    await adapter.record(
        SecurityMasterAuditEvent(
            context=context(),
            master_version=7,
            total_count=3,
            created_count=1,
            updated_count=1,
            revision_count=1,
        )
    )

    service_factory.assert_called_once_with(session)
    written = audit_service.append.await_args.args[0]
    assert written.action_code == "SECURITY_MASTER_APPLY"
    assert written.request_id == "req_12345678"
    assert written.actor_user_id == "user-1"
    assert written.session_id == "session-1"
    assert written.trusted_ip == "127.0.0.1"
    assert written.after_summary["master_version"] == 7


@pytest.mark.anyio
async def test_event_adapter_passes_the_same_session_to_the_outbox_writer() -> None:
    session = Mock()
    writer = Mock()
    writer.append = AsyncMock()
    adapter = TransactionalSecurityEventAdapter(session, writer)
    event = SecurityMasterUpdatedEvent(
        master_version=7,
        source="eastmoney",
        source_version="v7",
        total_count=3,
        created_count=1,
        updated_count=1,
    )

    await adapter.publish(event)

    arguments = writer.append.await_args.kwargs
    assert arguments["session"] is session
    assert arguments["topic"] == "security_master.updated"
    assert arguments["aggregate_id"] == "7"
    assert arguments["payload"]["source_version"] == "v7"
