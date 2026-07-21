import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from long_invest.modules.notifications.channels import ChannelResult
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    DeliveryOutcome,
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.delivery import (
    DeliveryAction,
    DeliveryDecision,
    RetryPolicy,
    aggregate_event_status,
)
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationEvent,
)
from long_invest.modules.notifications.repository import NotificationRepository
from long_invest.modules.notifications.security import validate_notification_payload
from long_invest.modules.notifications.template_catalog import (
    GIT_TEMPLATE_REGISTRY,
    TemplateRegistry,
    TemplateVersionNotFoundError,
)
from long_invest.modules.notifications.templates import StrictTemplateRenderer


@dataclass(frozen=True, slots=True)
class DeliveryLease:
    delivery_id: UUID
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class ChannelDeliveryTarget:
    channel: DeliveryChannel
    config_version: int
    target_fingerprint: str


@dataclass(frozen=True, slots=True)
class PublishNotification:
    event_type: str
    business_event_type: str
    business_event_id: str
    business_object_type: str
    business_object_id: str
    severity: str | None
    template_variables: dict[str, Any]
    template_version: str
    targets: tuple[ChannelDeliveryTarget, ...]
    idempotency_key: str
    request_id: str
    eligibility_status: NotificationEventStatus = NotificationEventStatus.ELIGIBLE
    suppression_reason: str | None = None


class NotificationPublishError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NotificationService:
    def __init__(
        self,
        repository: NotificationRepository,
        templates: TemplateRegistry = GIT_TEMPLATE_REGISTRY,
    ) -> None:
        self._repository = repository
        self._templates = templates
        self._retry_policy = RetryPolicy()

    async def publish(self, command: PublishNotification) -> NotificationEvent:
        content_hash = _publish_content_hash(command)
        existing = await self._repository.find_event_by_idempotency(
            command.idempotency_key
        )
        if existing is not None:
            return _resolve_publish_replay(existing, content_hash)

        try:
            definition = self._templates.resolve(
                command.event_type,
                command.template_version,
            )
        except TemplateVersionNotFoundError as exc:
            raise NotificationPublishError(
                code=exc.code,
                message=str(exc),
            ) from exc

        event_id = uuid4()
        template_variables = dict(command.template_variables)
        template_variables["event_id"] = str(event_id)
        StrictTemplateRenderer().render(definition, template_variables)
        is_eligible = command.eligibility_status == NotificationEventStatus.ELIGIBLE
        status = (
            NotificationEventStatus.DISPATCHED
            if is_eligible and command.targets
            else NotificationEventStatus.SUPPRESSED
        )
        event = NotificationEvent(
            id=event_id,
            event_type=command.event_type,
            business_event_type=command.business_event_type,
            business_event_id=command.business_event_id,
            business_object_type=command.business_object_type,
            business_object_id=command.business_object_id,
            severity=command.severity,
            template_variables=template_variables,
            status=status,
            eligibility_status=command.eligibility_status,
            suppression_reason=command.suppression_reason,
            effective_channels=[target.channel for target in command.targets],
            template_version=command.template_version,
            idempotency_key=command.idempotency_key,
            content_hash=content_hash,
            request_id=command.request_id,
        )
        deliveries = [
            NotificationDelivery(
                id=uuid4(),
                event_id=event_id,
                generation=1,
                channel=target.channel,
                config_version=target.config_version,
                target_fingerprint=target.target_fingerprint,
                status=NotificationDeliveryStatus.PENDING,
                attempt_count=0,
                unknown_compensation_count=0,
                deterministic_message_id=(
                    f"notification:{event_id}:{target.channel.value}:1"
                ),
            )
            for target in command.targets
            if is_eligible
        ]
        try:
            await self._repository.persist_event_and_deliveries(event, deliveries)
        except IntegrityError:
            existing = await self._repository.find_event_by_idempotency(
                command.idempotency_key
            )
            if existing is None:
                raise
            return _resolve_publish_replay(existing, content_hash)
        return event

    async def record_result(
        self,
        lease: DeliveryLease,
        *,
        result: ChannelResult,
        started_at: datetime,
        finished_at: datetime,
    ) -> bool:
        delivery = await self._repository.lock_delivery(lease.delivery_id)
        if (
            delivery is None
            or delivery.status != NotificationDeliveryStatus.SENDING
            or delivery.lease_token != lease.lease_token
        ):
            return False

        await self._record_locked_result(
            delivery,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
            phase="SEND",
        )
        await self._repository.flush()
        return True

    async def skip_delivery(
        self,
        lease: DeliveryLease,
        *,
        delivery_status: NotificationDeliveryStatus,
        reason: str,
        now: datetime,
    ) -> bool:
        del now
        delivery = await self._repository.lock_delivery(lease.delivery_id)
        if (
            delivery is None
            or delivery.status != NotificationDeliveryStatus.SENDING
            or delivery.lease_token != lease.lease_token
        ):
            return False
        delivery.status = delivery_status
        delivery.error_code = reason
        delivery.next_retry_at = None
        self._clear_lease(delivery)
        event = await self._repository.lock_event(delivery.event_id)
        if event is not None:
            deliveries = await self._repository.list_deliveries(event.id)
            event.status = aggregate_event_status(
                NotificationDeliveryStatus(item.status) for item in deliveries
            )
        await self._repository.flush()
        return True

    async def recover_expired_leases(
        self,
        *,
        channel: DeliveryChannel,
        now: datetime,
        limit: int,
    ) -> int:
        expired = await self._repository.lock_expired_leases(
            channel=channel,
            now=now,
            limit=limit,
        )
        ordered_expired = sorted(expired, key=lambda item: (item.event_id, item.id))
        for delivery in ordered_expired:
            await self._record_locked_result(
                delivery,
                result=ChannelResult.outcome_unknown(
                    code="DELIVERY_LEASE_EXPIRED",
                    summary="delivery worker lease expired during send",
                ),
                started_at=delivery.lease_expires_at or now,
                finished_at=now,
                phase="RECOVERY",
            )
        if expired:
            await self._repository.flush()
        return len(expired)

    async def _record_locked_result(
        self,
        delivery: NotificationDelivery,
        *,
        result: ChannelResult,
        started_at: datetime,
        finished_at: datetime,
        phase: str,
    ) -> None:
        event = await self._repository.lock_event(delivery.event_id)
        request_count = delivery.attempt_count + 1
        duration_ms = max(
            0,
            int((finished_at - started_at).total_seconds() * 1000),
        )
        self._repository.add_attempt(
            NotificationDeliveryAttempt(
                id=uuid4(),
                delivery_id=delivery.id,
                attempt_no=request_count,
                phase=phase,
                duration_ms=duration_ms,
                outcome=result.outcome,
                possibly_delivered=result.possibly_delivered,
                request_id=event.request_id if event is not None else "system_recovery",
                error_code=None
                if result.outcome is DeliveryOutcome.SUCCESS
                else result.code,
                response_summary=result.as_safe_dict(),
                started_at=started_at,
                finished_at=finished_at,
            )
        )
        delivery.attempt_count = request_count
        if (
            delivery.unknown_compensation_count >= 1
            and result.outcome is not DeliveryOutcome.SUCCESS
        ):
            action = (
                DeliveryAction.KEEP_UNKNOWN
                if result.outcome is DeliveryOutcome.OUTCOME_UNKNOWN
                else DeliveryAction.FAIL
            )
            decision = DeliveryDecision(action, "UNKNOWN_COMPENSATION_FINISHED")
        else:
            decision = self._retry_policy.decide(
                outcome=result.outcome,
                request_count=request_count,
                unknown_compensation_count=delivery.unknown_compensation_count,
            )
        self._apply_decision(delivery, result, decision, finished_at)
        self._clear_lease(delivery)

        if event is not None:
            deliveries = await self._repository.list_deliveries(event.id)
            event.status = aggregate_event_status(
                NotificationDeliveryStatus(item.status) for item in deliveries
            )

    @staticmethod
    def _apply_decision(
        delivery: NotificationDelivery,
        result: ChannelResult,
        decision: DeliveryDecision,
        now: datetime,
    ) -> None:
        delivery.error_code = (
            None if result.outcome is DeliveryOutcome.SUCCESS else result.code
        )
        if decision.action is DeliveryAction.SENT:
            delivery.status = NotificationDeliveryStatus.SENT
            delivery.sent_at = now
            delivery.next_retry_at = None
        elif decision.action is DeliveryAction.RETRY:
            delivery.status = (
                NotificationDeliveryStatus.OUTCOME_UNKNOWN
                if result.outcome is DeliveryOutcome.OUTCOME_UNKNOWN
                else NotificationDeliveryStatus.RETRY_WAIT
            )
            delivery.next_retry_at = now + timedelta(
                seconds=decision.delay_seconds or 0
            )
            if decision.consume_unknown_compensation:
                delivery.unknown_compensation_count += 1
        elif decision.action is DeliveryAction.KEEP_UNKNOWN:
            delivery.status = NotificationDeliveryStatus.OUTCOME_UNKNOWN
            delivery.next_retry_at = None
        else:
            delivery.status = NotificationDeliveryStatus.FAILED
            delivery.next_retry_at = None

    @staticmethod
    def _clear_lease(delivery: NotificationDelivery) -> None:
        delivery.lease_owner = None
        delivery.lease_token = None
        delivery.lease_expires_at = None


def transactional_notification_service(session) -> NotificationService:
    return NotificationService(NotificationRepository(session))


def _publish_content_hash(command: PublishNotification) -> str:
    content = {
        "event_type": command.event_type,
        "business_event_type": command.business_event_type,
        "business_event_id": command.business_event_id,
        "business_object_type": command.business_object_type,
        "business_object_id": command.business_object_id,
        "severity": command.severity,
        "template_variables": validate_notification_payload(command.template_variables),
        "template_version": command.template_version,
        "targets": sorted(
            (
                target.channel.value,
                target.config_version,
                target.target_fingerprint,
            )
            for target in command.targets
        ),
        "eligibility_status": command.eligibility_status.value,
        "suppression_reason": command.suppression_reason,
    }
    serialized = json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _resolve_publish_replay(
    existing: NotificationEvent,
    content_hash: str,
) -> NotificationEvent:
    if existing.content_hash != content_hash:
        raise NotificationPublishError(
            code="NOTIFICATION_IDEMPOTENCY_KEY_REUSED",
            message="notification idempotency key was reused for other content",
        )
    return existing
