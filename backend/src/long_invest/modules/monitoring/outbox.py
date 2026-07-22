from typing import Any

from long_invest.platform.outbox.service import TransactionalOutboxWriter


class MonitorSubscriptionOutbox:
    def __init__(self, session, writer=None):
        self.session = session
        self.writer = writer or TransactionalOutboxWriter()

    async def publish(self, event: Any) -> None:
        topic_action = {
            "paused": "disabled",
            "restored": "changed",
            "notification_policy_changed": "changed",
        }.get(event.action, event.action)
        await self.writer.append(
            session=self.session,
            topic=f"monitor_subscription.{topic_action}",
            aggregate_type="monitor_subscription",
            aggregate_id=str(event.subscription_id),
            queue="domain-events",
            payload={
                "event_type": f"monitor_subscription.{topic_action}",
                "subscription_id": str(event.subscription_id),
                "security_id": str(event.security_id),
                "symbol": event.symbol,
                "status": str(event.status),
                "version": event.version,
                "revision_id": str(event.revision_id),
                "action": event.action,
                "reason": event.reason,
                "notification_mode": event.after_summary["notification_mode"],
                "notification_channels": event.after_summary["notification_channels"],
            },
            dedupe_key=f"monitor-subscription:{event.subscription_id}:{event.version}:{event.action}",
        )
