from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from long_invest.modules.notifications.channels import (
    ChannelResult,
    ChannelSendRequest,
    NotificationChannel,
)
from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.eligibility import EligibilityDecision
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
)
from long_invest.modules.notifications.service import DeliveryLease
from long_invest.modules.notifications.template_catalog import (
    GIT_TEMPLATE_REGISTRY,
    TemplateRegistry,
    TemplateVersionNotFoundError,
)
from long_invest.modules.notifications.templates import TemplateRenderError

EligibilityReviewer = Callable[
    [NotificationEvent, NotificationDelivery],
    Awaitable[EligibilityDecision],
]


@dataclass(frozen=True, slots=True)
class ClaimedNotificationDelivery:
    delivery: NotificationDelivery
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class NotificationExecution:
    lease: DeliveryLease
    started_at: datetime
    finished_at: datetime
    result: ChannelResult | None
    skip_decision: EligibilityDecision | None


class NotificationWorker:
    def __init__(
        self,
        *,
        channel: NotificationChannel,
        eligibility_reviewer: EligibilityReviewer,
        templates: TemplateRegistry = GIT_TEMPLATE_REGISTRY,
    ) -> None:
        self._channel = channel
        self._eligibility_reviewer = eligibility_reviewer
        self._templates = templates

    async def execute_claimed(
        self,
        claimed: ClaimedNotificationDelivery,
        event: NotificationEvent,
        *,
        started_at: datetime,
    ) -> NotificationExecution:
        delivery = claimed.delivery
        lease = DeliveryLease(delivery.id, claimed.lease_token)
        if DeliveryChannel(delivery.channel) is not self._channel.channel:
            return self._result(
                lease,
                started_at,
                ChannelResult.permanent_failure(
                    code="DELIVERY_CHANNEL_MISMATCH",
                    summary="delivery belongs to another notification channel",
                ),
            )

        eligibility = await self._eligibility_reviewer(event, delivery)
        if not eligibility.eligible:
            return NotificationExecution(
                lease=lease,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                result=None,
                skip_decision=eligibility,
            )

        try:
            definition = self._templates.resolve(
                event.event_type,
                event.template_version,
            )
            rendered = self._channel.render(definition, event.template_variables)
            result = await self._channel.send(
                ChannelSendRequest(
                    event_id=str(event.id),
                    deterministic_message_id=delivery.deterministic_message_id,
                    subject=rendered.subject,
                    text=rendered.text,
                    html=rendered.html,
                )
            )
        except (TemplateVersionNotFoundError, TemplateRenderError) as exc:
            result = ChannelResult.permanent_failure(
                code="NOTIFICATION_TEMPLATE_INVALID",
                summary=str(exc),
            )
        except Exception as exc:
            result = ChannelResult.temporary_failure(
                code="NOTIFICATION_CHANNEL_EXECUTION_FAILED",
                summary="notification channel execution failed",
                details={"error_type": type(exc).__name__},
            )
        return self._result(lease, started_at, result)

    @staticmethod
    def _result(
        lease: DeliveryLease,
        started_at: datetime,
        result: ChannelResult,
    ) -> NotificationExecution:
        return NotificationExecution(
            lease=lease,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            result=result,
            skip_decision=None,
        )
