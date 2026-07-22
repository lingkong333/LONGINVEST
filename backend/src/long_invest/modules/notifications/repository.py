from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
)
from long_invest.modules.notifications.models import (
    NotificationChannelCircuit,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationEvent,
    NotificationTemplateActivation,
    NotificationTemplateVersion,
)
from long_invest.modules.notifications.resource_events import (
    NotificationResourceEvents,
)
from long_invest.modules.notifications.template_catalog import TemplateRegistry
from long_invest.modules.notifications.templates import TemplateDefinition


class TemplateVersionConflictError(RuntimeError):
    code = "NOTIFICATION_TEMPLATE_VERSION_CONFLICT"


def _template_content_hash(definition: TemplateDefinition) -> str:
    import hashlib
    import json

    serialized = json.dumps(
        {
            "html": definition.html,
            "subject": definition.subject,
            "text": definition.text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    delivery: NotificationDelivery
    lease_token: UUID


class NotificationRepository:
    def __init__(
        self,
        session: AsyncSession,
        resource_events: NotificationResourceEvents | None = None,
    ) -> None:
        self._session = session
        self.resource_events = resource_events or NotificationResourceEvents(session)

    async def find_event_by_idempotency(
        self,
        idempotency_key: str,
    ) -> NotificationEvent | None:
        return await self._session.scalar(
            select(NotificationEvent).where(
                NotificationEvent.idempotency_key == idempotency_key
            )
        )

    async def resolve_active_template(
        self,
        template_type: str,
        registry: TemplateRegistry,
    ) -> TemplateDefinition | None:
        definition = await self._read_active_template(template_type)
        if definition is not None:
            return definition
        return next(
            (
                candidate
                for candidate in registry.definitions.values()
                if candidate.template_type == template_type
            ),
            None,
        )

    async def sync_templates(self, registry: TemplateRegistry) -> None:
        first_versions: dict[str, str] = {}
        for definition in registry.definitions.values():
            first_versions.setdefault(definition.template_type, definition.version)
            content_hash = _template_content_hash(definition)
            await self._session.execute(
                insert(NotificationTemplateVersion)
                .values(
                    id=uuid4(),
                    template_type=definition.template_type,
                    version=definition.version,
                    subject=definition.subject,
                    text=definition.text,
                    html=definition.html,
                    content_hash=content_hash,
                    source="GIT",
                )
                .on_conflict_do_nothing(index_elements=["template_type", "version"])
            )
            persisted = await self._session.scalar(
                select(NotificationTemplateVersion).where(
                    NotificationTemplateVersion.template_type
                    == definition.template_type,
                    NotificationTemplateVersion.version == definition.version,
                )
            )
            if persisted is not None and persisted.content_hash != content_hash:
                raise TemplateVersionConflictError(
                    "Git template content changed for an existing immutable version: "
                    f"{definition.template_type}@{definition.version}"
                )
        for template_type, version in first_versions.items():
            await self._session.execute(
                insert(NotificationTemplateActivation)
                .values(template_type=template_type, active_version=version)
                .on_conflict_do_nothing(index_elements=["template_type"])
            )
        await self._session.flush()

    async def activate_template(
        self,
        template_type: str,
        version: str,
    ) -> NotificationTemplateActivation | None:
        selected = await self._session.scalar(
            select(NotificationTemplateVersion).where(
                NotificationTemplateVersion.template_type == template_type,
                NotificationTemplateVersion.version == version,
            )
        )
        if selected is None:
            return None
        activation = await self._session.scalar(
            select(NotificationTemplateActivation)
            .where(NotificationTemplateActivation.template_type == template_type)
            .with_for_update()
        )
        if activation is None:
            activation = NotificationTemplateActivation(
                template_type=template_type,
                active_version=version,
            )
            self._session.add(activation)
        else:
            activation.active_version = version
        await self._session.flush()
        return activation

    async def list_template_versions(self) -> list[NotificationTemplateVersion]:
        rows = await self._session.scalars(
            select(NotificationTemplateVersion).order_by(
                NotificationTemplateVersion.template_type,
                NotificationTemplateVersion.created_at,
                NotificationTemplateVersion.version,
            )
        )
        return list(rows.all())

    async def active_template_versions(self) -> dict[str, str]:
        rows = await self._session.execute(
            select(
                NotificationTemplateActivation.template_type,
                NotificationTemplateActivation.active_version,
            )
        )
        return {str(template_type): str(version) for template_type, version in rows}

    async def read_template_version(
        self,
        template_type: str,
        version: str,
    ) -> TemplateDefinition | None:
        row = await self._session.scalar(
            select(NotificationTemplateVersion).where(
                NotificationTemplateVersion.template_type == template_type,
                NotificationTemplateVersion.version == version,
            )
        )
        if row is None:
            return None
        return TemplateDefinition(
            template_type=row.template_type,
            version=row.version,
            subject=row.subject,
            text=row.text,
            html=row.html,
        )

    async def _read_active_template(
        self,
        template_type: str,
    ) -> TemplateDefinition | None:
        row = await self._session.scalar(
            select(NotificationTemplateVersion)
            .join(
                NotificationTemplateActivation,
                and_(
                    NotificationTemplateActivation.template_type
                    == NotificationTemplateVersion.template_type,
                    NotificationTemplateActivation.active_version
                    == NotificationTemplateVersion.version,
                ),
            )
            .where(NotificationTemplateVersion.template_type == template_type)
        )
        if row is None:
            return None
        return TemplateDefinition(
            template_type=row.template_type,
            version=row.version,
            subject=row.subject,
            text=row.text,
            html=row.html,
        )

    async def lock_channel_circuit(
        self,
        channel: DeliveryChannel,
        instance: str,
    ) -> NotificationChannelCircuit:
        await self._session.execute(
            insert(NotificationChannelCircuit)
            .values(
                id=uuid4(),
                channel=channel.value,
                instance=instance,
                state="CLOSED",
                consecutive_failures=0,
                cooldown_level=0,
            )
            .on_conflict_do_nothing(index_elements=["channel", "instance"])
        )
        circuit = await self._session.scalar(
            select(NotificationChannelCircuit)
            .where(
                NotificationChannelCircuit.channel == channel.value,
                NotificationChannelCircuit.instance == instance,
            )
            .with_for_update()
        )
        if circuit is None:
            raise RuntimeError("notification channel circuit could not be created")
        return circuit

    async def read_channel_circuit(
        self,
        channel: DeliveryChannel,
        instance: str,
    ) -> NotificationChannelCircuit | None:
        return await self._session.scalar(
            select(NotificationChannelCircuit).where(
                NotificationChannelCircuit.channel == channel.value,
                NotificationChannelCircuit.instance == instance,
            )
        )

    async def defer_channel_deliveries(
        self,
        *,
        channel: DeliveryChannel,
        until: datetime,
    ) -> None:
        await self._session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.channel == channel.value,
                NotificationDelivery.status == NotificationDeliveryStatus.PENDING,
            )
            .values(
                status=NotificationDeliveryStatus.RETRY_WAIT,
                next_retry_at=until,
                circuit_deferred_until=until,
            )
        )
        await self._session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.channel == channel.value,
                NotificationDelivery.status.in_(
                    (
                        NotificationDeliveryStatus.RETRY_WAIT,
                        NotificationDeliveryStatus.OUTCOME_UNKNOWN,
                    )
                ),
                or_(
                    NotificationDelivery.circuit_deferred_until.is_(None),
                    NotificationDelivery.circuit_deferred_until < until,
                ),
            )
            .values(circuit_deferred_until=until)
        )

    async def release_channel_deliveries(
        self,
        *,
        channel: DeliveryChannel,
    ) -> None:
        await self._session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.channel == channel.value,
                NotificationDelivery.status == NotificationDeliveryStatus.RETRY_WAIT,
                NotificationDelivery.attempt_count == 0,
                NotificationDelivery.circuit_deferred_until.is_not(None),
            )
            .values(
                status=NotificationDeliveryStatus.PENDING,
                next_retry_at=None,
                circuit_deferred_until=None,
            )
        )
        await self._session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.channel == channel.value,
                NotificationDelivery.circuit_deferred_until.is_not(None),
            )
            .values(circuit_deferred_until=None)
        )

    async def persist_event_and_deliveries(
        self,
        event: NotificationEvent,
        deliveries: list[NotificationDelivery],
    ) -> None:
        async with self._session.begin_nested():
            self._session.add(event)
            await self._session.flush([event])
            self._session.add_all(deliveries)
            await self._session.flush(deliveries)
            await self.resource_events.event_changed(
                event,
                change=("suppressed" if event.status == "SUPPRESSED" else "requested"),
                dedupe_token="created",
            )
            for delivery in deliveries:
                await self.resource_events.delivery_changed(
                    delivery,
                    request_id=event.request_id,
                    change="created",
                    dedupe_token=f"generation-{delivery.generation}",
                )

    async def claim_next(
        self,
        *,
        channel: DeliveryChannel,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ClaimedDelivery | None:
        due = or_(
            and_(
                NotificationDelivery.status == NotificationDeliveryStatus.PENDING,
                or_(
                    NotificationDelivery.circuit_deferred_until.is_(None),
                    NotificationDelivery.circuit_deferred_until <= now,
                ),
            ),
            and_(
                NotificationDelivery.status == NotificationDeliveryStatus.RETRY_WAIT,
                NotificationDelivery.next_retry_at <= now,
                or_(
                    NotificationDelivery.circuit_deferred_until.is_(None),
                    NotificationDelivery.circuit_deferred_until <= now,
                ),
            ),
            and_(
                NotificationDelivery.status
                == NotificationDeliveryStatus.OUTCOME_UNKNOWN,
                NotificationDelivery.next_retry_at <= now,
                or_(
                    NotificationDelivery.circuit_deferred_until.is_(None),
                    NotificationDelivery.circuit_deferred_until <= now,
                ),
            ),
        )
        result = await self._session.execute(
            select(NotificationDelivery)
            .where(NotificationDelivery.channel == channel, due)
            .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        delivery = result.scalars().first()
        if delivery is None:
            return None

        lease_token = uuid4()
        delivery.status = NotificationDeliveryStatus.SENDING
        delivery.lease_owner = worker_id
        delivery.lease_token = lease_token
        delivery.lease_expires_at = now + lease_for
        await self._session.flush()
        await self.resource_events.delivery_changed(
            delivery,
            request_id=None,
            change="started",
            dedupe_token=(
                f"generation-{delivery.generation}-attempt-{delivery.attempt_count + 1}"
            ),
        )
        return ClaimedDelivery(delivery, lease_token)

    async def lock_expired_leases(
        self,
        *,
        channel: DeliveryChannel,
        now: datetime,
        limit: int,
    ) -> list[NotificationDelivery]:
        result = await self._session.execute(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.channel == channel,
                NotificationDelivery.status == NotificationDeliveryStatus.SENDING,
                NotificationDelivery.lease_expires_at <= now,
            )
            .order_by(NotificationDelivery.event_id, NotificationDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())

    async def lock_delivery(self, delivery_id: UUID) -> NotificationDelivery | None:
        return await self._session.scalar(
            select(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id)
            .with_for_update()
        )

    async def get_event(self, event_id: UUID) -> NotificationEvent | None:
        return await self._session.get(NotificationEvent, event_id)

    async def lock_event(self, event_id: UUID) -> NotificationEvent | None:
        return await self._session.scalar(
            select(NotificationEvent)
            .where(NotificationEvent.id == event_id)
            .with_for_update()
        )

    async def list_deliveries(self, event_id: UUID) -> list[NotificationDelivery]:
        result = await self._session.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.event_id == event_id)
            .execution_options(populate_existing=True)
        )
        return list(result.all())

    def add_attempt(self, attempt: NotificationDeliveryAttempt) -> None:
        self._session.add(attempt)

    async def flush(self) -> None:
        await self._session.flush()
