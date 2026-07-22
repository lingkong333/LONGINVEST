from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

import long_invest.modules.notifications.application as application_module
from long_invest.modules.notifications.application import NotificationAdminApplication
from long_invest.platform.errors import AppError


class Transaction:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args) -> None:
        return None


class Database:
    def __init__(self, session) -> None:
        self.session = session

    def transaction(self):
        return Transaction(self.session)


@pytest.mark.anyio
async def test_risky_resend_idempotency_replay_reuses_created_generation(
    monkeypatch,
) -> None:
    source_id = uuid4()
    created_id = uuid4()
    created_delivery = SimpleNamespace(id=created_id)
    session = SimpleNamespace(get=AsyncMock(return_value=created_delivery))
    audit = SimpleNamespace(
        find_by_idempotency=AsyncMock(
            return_value=SimpleNamespace(
                after_summary={
                    "method": "retry_delivery",
                    "delivery_id": str(created_id),
                    "source_delivery_id": str(source_id),
                    "confirm_duplicate_risk": True,
                }
            )
        )
    )
    service_factory = Mock(side_effect=AssertionError("service must not run on replay"))
    monkeypatch.setattr(application_module, "AuditService", lambda _session: audit)
    monkeypatch.setattr(application_module, "NotificationAdminService", service_factory)

    result = await NotificationAdminApplication(Database(session)).mutate(
        "retry_delivery",
        source_id,
        True,
        request_id="request-1",
        idempotency_key="resend-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        reason="人工确认可能重复后重发",
    )

    assert result.delivery is created_delivery
    assert result.changed is False
    service_factory.assert_not_called()


@pytest.mark.anyio
async def test_risky_resend_idempotency_rejects_changed_confirmation(
    monkeypatch,
) -> None:
    source_id = uuid4()
    created_id = uuid4()
    session = SimpleNamespace(get=AsyncMock())
    audit = SimpleNamespace(
        find_by_idempotency=AsyncMock(
            return_value=SimpleNamespace(
                after_summary={
                    "method": "retry_delivery",
                    "delivery_id": str(created_id),
                    "source_delivery_id": str(source_id),
                    "confirm_duplicate_risk": True,
                }
            )
        )
    )
    monkeypatch.setattr(application_module, "AuditService", lambda _session: audit)

    with pytest.raises(AppError) as exc_info:
        await NotificationAdminApplication(Database(session)).mutate(
            "retry_delivery",
            source_id,
            False,
            request_id="request-1",
            idempotency_key="resend-1",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
            reason="不同请求",
        )

    assert exc_info.value.code == "NOTIFICATION_IDEMPOTENCY_CONFLICT"
    session.get.assert_not_awaited()
