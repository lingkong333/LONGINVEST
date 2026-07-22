from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.monitoring.outbox import MonitorSubscriptionOutbox


class Writer:
    def __init__(self) -> None:
        self.calls = []

    async def append(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.mark.anyio
async def test_notification_policy_event_freezes_selection_in_outbox() -> None:
    writer = Writer()
    subscription_id = uuid4()
    event = SimpleNamespace(
        action="notification_policy_changed",
        subscription_id=subscription_id,
        security_id=uuid4(),
        symbol="600000.SH",
        status="ENABLED",
        version=3,
        revision_id=uuid4(),
        reason="单股仅使用邮件",
        after_summary={
            "notification_mode": "CUSTOM",
            "notification_channels": ["EMAIL"],
        },
    )

    await MonitorSubscriptionOutbox(object(), writer=writer).publish(event)

    call = writer.calls[0]
    assert call["topic"] == "monitor_subscription.changed"
    assert call["payload"]["notification_mode"] == "CUSTOM"
    assert call["payload"]["notification_channels"] == ["EMAIL"]
    assert call["dedupe_key"].endswith(":3:notification_policy_changed")
