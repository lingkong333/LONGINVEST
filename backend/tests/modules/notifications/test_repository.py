import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
)
from long_invest.modules.notifications.models import NotificationDelivery


def load_repository():
    module_name = "long_invest.modules.notifications.repository"
    assert importlib.util.find_spec(module_name) is not None, (
        "notification SQLAlchemy repository is not implemented"
    )
    return importlib.import_module(module_name)


def pending_delivery() -> NotificationDelivery:
    return NotificationDelivery(
        id=uuid4(),
        event_id=uuid4(),
        generation=1,
        channel=DeliveryChannel.WECOM,
        config_version=1,
        target_fingerprint="wecom:primary",
        status=NotificationDeliveryStatus.PENDING,
        attempt_count=0,
        unknown_compensation_count=0,
        deterministic_message_id=f"message-{uuid4()}",
    )


@pytest.mark.anyio
async def test_claim_next_uses_skip_locked_and_sets_a_fenced_lease() -> None:
    repository = load_repository()
    now = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    delivery = pending_delivery()
    session = AsyncMock()
    result = Mock()
    result.scalars.return_value.first.return_value = delivery
    session.execute.return_value = result

    claimed = await repository.NotificationRepository(session).claim_next(
        channel=DeliveryChannel.WECOM,
        worker_id="notify-wecom-1",
        now=now,
        lease_for=timedelta(seconds=30),
    )

    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert claimed is not None
    assert claimed.delivery is delivery
    assert claimed.lease_token == delivery.lease_token
    assert delivery.status == NotificationDeliveryStatus.SENDING
    assert delivery.lease_owner == "notify-wecom-1"
    assert delivery.lease_expires_at == now + timedelta(seconds=30)
    session.flush.assert_awaited_once()


@pytest.mark.anyio
async def test_repository_locks_only_expired_sending_leases_for_recovery() -> None:
    repository = load_repository()
    now = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    delivery = pending_delivery()
    delivery.status = NotificationDeliveryStatus.SENDING
    delivery.lease_owner = "crashed-worker"
    delivery.lease_token = uuid4()
    delivery.lease_expires_at = now - timedelta(seconds=1)
    session = AsyncMock()
    result = Mock()
    result.scalars.return_value.all.return_value = [delivery]
    session.execute.return_value = result

    expired = await repository.NotificationRepository(session).lock_expired_leases(
        channel=DeliveryChannel.WECOM,
        now=now,
        limit=10,
    )

    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "NOTIFICATION_DELIVERY.STATUS" in sql
    assert "LEASE_EXPIRES_AT" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert expired == [delivery]
