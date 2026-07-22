from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from long_invest.modules.notifications.channels import (
    ChannelResult,
    ChannelSendRequest,
    EmailChannelConfig,
    WeComChannelConfig,
)
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.eligibility import EligibilityDecision
from long_invest.modules.notifications.email import SmtpEmailChannel
from long_invest.modules.notifications.repository import NotificationRepository
from long_invest.modules.notifications.security import SecretReferenceValue
from long_invest.modules.notifications.service import DeliveryLease, NotificationService
from long_invest.modules.notifications.targets import target_fingerprint
from long_invest.modules.notifications.template_catalog import GIT_TEMPLATE_REGISTRY
from long_invest.modules.notifications.wecom import WeComRobotChannel
from long_invest.modules.notifications.worker import (
    ClaimedNotificationDelivery,
    NotificationWorker,
)
from long_invest.modules.settings.crypto import SecretCipher
from long_invest.modules.settings.service import transactional_settings_service
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database

_CONFIG_KEYS = {
    DeliveryChannel.WECOM: "notification.channel.wecom",
    DeliveryChannel.EMAIL: "notification.channel.email",
}
_SECRET_KEYS = {
    DeliveryChannel.WECOM: "notification.wecom.webhook",
    DeliveryChannel.EMAIL: "notification.email.password",
}


class NotificationDeliveryRuntime:
    def __init__(
        self,
        database: Database,
        settings: AppSettings,
        *,
        worker_id: str | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._worker_id = worker_id or f"notification-{uuid4()}"
        self._cipher = (
            SecretCipher(settings.master_key) if settings.master_key else None
        )

    async def process_once(self, channel: DeliveryChannel) -> bool:
        now = datetime.now(UTC)
        async with self._database.transaction() as session:
            repository = NotificationRepository(session)
            service = NotificationService(repository)
            await service.recover_expired_leases(channel=channel, now=now, limit=20)
            claimed = await repository.claim_next(
                channel=channel,
                worker_id=self._worker_id,
                now=now,
                lease_for=timedelta(
                    seconds=self._settings.notification_worker_lease_seconds
                ),
            )
            if claimed is None:
                return False
            event = await repository.get_event(claimed.delivery.event_id)
            if event is None:
                await service.skip_delivery(
                    lease=_lease(claimed),
                    delivery_status=NotificationDeliveryStatus.SKIPPED_INELIGIBLE,
                    reason="NOTIFICATION_EVENT_NOT_FOUND",
                    now=now,
                )
                return True

        config, secret_status, secret = await self._load_channel(channel)
        reviewer = self._reviewer(config, secret_status)
        eligibility = await reviewer(event, claimed.delivery)
        if not eligibility.eligible:
            async with self._database.transaction() as session:
                service = NotificationService(NotificationRepository(session))
                await service.skip_delivery(
                    _lease(claimed),
                    delivery_status=NotificationDeliveryStatus(
                        eligibility.delivery_status
                        or NotificationDeliveryStatus.SKIPPED_INELIGIBLE
                    ),
                    reason=eligibility.reason or "INELIGIBLE",
                    now=datetime.now(UTC),
                )
            return True

        async def eligible(_event, _delivery):
            return EligibilityDecision(True, None, None)

        try:
            async with self._build_channel(channel, config, secret) as sender:
                execution = await NotificationWorker(
                    channel=sender,
                    eligibility_reviewer=eligible,
                ).execute_claimed(
                    ClaimedNotificationDelivery(claimed.delivery, claimed.lease_token),
                    event,
                    started_at=now,
                )
        except ValueError as exc:
            await self._record_build_failure(
                claimed,
                now,
                ChannelResult.permanent_failure(
                    code="NOTIFICATION_CHANNEL_CONFIG_INVALID",
                    summary=str(exc),
                ),
            )
            return True
        except Exception as exc:
            await self._record_build_failure(
                claimed,
                now,
                ChannelResult.temporary_failure(
                    code="NOTIFICATION_CHANNEL_BUILD_FAILED",
                    summary="notification channel could not be initialized",
                    details={"error_type": type(exc).__name__},
                ),
            )
            return True
        async with self._database.transaction() as session:
            service = NotificationService(NotificationRepository(session))
            if execution.skip_decision is not None:
                status = NotificationDeliveryStatus(
                    execution.skip_decision.delivery_status
                    or NotificationDeliveryStatus.SKIPPED_INELIGIBLE
                )
                await service.skip_delivery(
                    execution.lease,
                    delivery_status=status,
                    reason=execution.skip_decision.reason or "INELIGIBLE",
                    now=execution.finished_at,
                )
            else:
                assert execution.result is not None
                await service.record_result(
                    execution.lease,
                    result=execution.result,
                    started_at=execution.started_at,
                    finished_at=execution.finished_at,
                )
        return True

    async def test_channel(
        self, channel: DeliveryChannel, *, message: str
    ) -> ChannelResult:
        config, secret_status, secret = await self._load_channel(channel)
        if not config["value"]["enabled"] or not secret_status["configured"]:
            return ChannelResult.permanent_failure(
                code="NOTIFICATION_CHANNEL_DISABLED",
                summary="notification channel is disabled or incomplete",
            )
        event_id = str(uuid4())
        definition = GIT_TEMPLATE_REGISTRY.resolve("notification.test", "v1")
        async with self._build_channel(channel, config, secret) as sender:
            rendered = sender.render(
                definition,
                {"message": message, "event_id": event_id},
            )
            return await sender.test(
                ChannelSendRequest(
                    event_id=event_id,
                    deterministic_message_id=f"notification-test:{event_id}",
                    subject=rendered.subject,
                    text=rendered.text,
                    html=rendered.html,
                )
            )

    async def _record_build_failure(self, claimed, started_at, result) -> None:
        async with self._database.transaction() as session:
            service = NotificationService(NotificationRepository(session))
            await service.record_result(
                _lease(claimed),
                result=result,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

    async def _load_channel(self, channel: DeliveryChannel):
        async with self._database.session() as session:
            service = transactional_settings_service(session, cipher=self._cipher)
            config = await service.get_setting(_CONFIG_KEYS[channel])
            statuses = {item["key"]: item for item in await service.secret_statuses()}
            secret_status = statuses[_SECRET_KEYS[channel]]
            secret = None
            if secret_status["configured"] and self._cipher is not None:
                secret = await service.resolve_secret(_SECRET_KEYS[channel])
            return config, secret_status, secret

    def _reviewer(self, config: dict[str, Any], secret: dict[str, Any]):
        async def review(event, delivery) -> EligibilityDecision:
            if (
                NotificationEventStatus(event.status)
                is NotificationEventStatus.CANCELED
            ):
                return EligibilityDecision(False, "EVENT_CANCELED", "CANCELED")
            current_fingerprint = target_fingerprint(
                DeliveryChannel(delivery.channel), config, secret
            )
            available = (
                config["value"]["enabled"]
                and secret["configured"]
                and self._cipher is not None
                and delivery.config_version == config["version"]
                and delivery.target_fingerprint == current_fingerprint
            )
            if not available:
                return EligibilityDecision(
                    False, "CHANNEL_CONFIGURATION_CHANGED", "SKIPPED_DISABLED"
                )
            if event.event_type in {
                "signal.high",
                "signal.high_cleared",
            } and not event.template_variables.get("holding", False):
                return EligibilityDecision(False, "NOT_HOLDING", "SKIPPED_INELIGIBLE")
            return EligibilityDecision(True, None, None)

        return review

    def _build_channel(self, channel, config, secret):
        if not secret:
            raise RuntimeError("notification channel secret is not configured")
        value = config["value"]
        secret_ref = SecretReferenceValue(f"secret://{_SECRET_KEYS[channel]}")
        if channel is DeliveryChannel.WECOM:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(value["timeout_seconds"]),
                follow_redirects=False,
            )
            sender = WeComRobotChannel(
                config=WeComChannelConfig(
                    config_version=config["version"],
                    target_fingerprint="runtime",
                    webhook_secret_ref=secret_ref,
                ),
                webhook_url=secret,
                client=client,
            )
            return _AsyncChannelContext(sender, client=client)
        allowed_hosts = tuple(
            item.strip()
            for item in self._settings.notification_allowed_smtp_hosts.split(",")
            if item.strip()
        )
        sender = SmtpEmailChannel(
            config=EmailChannelConfig(
                config_version=config["version"],
                target_fingerprint="runtime",
                password_secret_ref=secret_ref,
                sender=value["sender"],
                recipients=tuple(value["recipients"]),
            ),
            smtp_host=value["smtp_host"],
            smtp_port=value["smtp_port"],
            security=value["security"],
            allowed_hosts=allowed_hosts,
            username=value["username"] or None,
            password=secret,
            timeout_seconds=value["timeout_seconds"],
        )
        return _AsyncChannelContext(sender)


class _AsyncChannelContext:
    def __init__(self, channel, *, client=None) -> None:
        self.channel = channel
        self.client = client

    async def __aenter__(self):
        return self.channel

    async def __aexit__(self, *_args) -> None:
        if self.client is not None:
            await self.client.aclose()


def _lease(claimed):
    return DeliveryLease(claimed.delivery.id, claimed.lease_token)
