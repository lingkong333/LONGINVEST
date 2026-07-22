from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationDeliveryStatus,
    NotificationEventStatus,
)
from long_invest.modules.notifications.delivery import aggregate_event_status
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationEvent,
)

_CANCELABLE_STATUSES = {
    NotificationDeliveryStatus.PENDING,
    NotificationDeliveryStatus.RETRY_WAIT,
}
_MANUALLY_RETRYABLE_STATUSES = {NotificationDeliveryStatus.FAILED}


@dataclass(frozen=True, slots=True)
class AdminPage[T]:
    items: tuple[T, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class NotificationEventDetail:
    event: NotificationEvent
    deliveries: tuple[NotificationDelivery, ...]


@dataclass(frozen=True, slots=True)
class DeliveryMutation:
    delivery: NotificationDelivery
    changed: bool


@dataclass(frozen=True, slots=True)
class DeliveryRetryFailure:
    delivery_id: UUID
    code: str


@dataclass(frozen=True, slots=True)
class DeliveryRetryBatch:
    retried: tuple[NotificationDelivery, ...]
    failures: tuple[DeliveryRetryFailure, ...]


class NotificationAdminError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NotificationAdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_events(
        self,
        *,
        page: int,
        page_size: int,
        status: NotificationEventStatus | None = None,
        event_type: str | None = None,
    ) -> AdminPage[NotificationEvent]:
        _validate_page(page, page_size)
        statement = select(NotificationEvent)
        if status is not None:
            statement = statement.where(NotificationEvent.status == status)
        if event_type is not None:
            statement = statement.where(NotificationEvent.event_type == event_type)
        return await self._page(
            statement,
            page=page,
            page_size=page_size,
            order_by=(NotificationEvent.created_at.desc(), NotificationEvent.id.desc()),
        )

    async def get_event_detail(
        self,
        event_id: UUID,
    ) -> NotificationEventDetail | None:
        event = await self._session.get(NotificationEvent, event_id)
        if event is None:
            return None
        result = await self._session.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.event_id == event_id)
            .order_by(NotificationDelivery.channel, NotificationDelivery.generation)
        )
        return NotificationEventDetail(event=event, deliveries=tuple(result.all()))

    async def list_deliveries(
        self,
        *,
        page: int,
        page_size: int,
        event_id: UUID | None = None,
        status: NotificationDeliveryStatus | None = None,
        channel: DeliveryChannel | None = None,
    ) -> AdminPage[NotificationDelivery]:
        _validate_page(page, page_size)
        statement = select(NotificationDelivery)
        if event_id is not None:
            statement = statement.where(NotificationDelivery.event_id == event_id)
        if status is not None:
            statement = statement.where(NotificationDelivery.status == status)
        if channel is not None:
            statement = statement.where(NotificationDelivery.channel == channel)
        return await self._page(
            statement,
            page=page,
            page_size=page_size,
            order_by=(
                NotificationDelivery.created_at.desc(),
                NotificationDelivery.id.desc(),
            ),
        )

    async def list_attempts(
        self,
        delivery_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> AdminPage[NotificationDeliveryAttempt]:
        _validate_page(page, page_size)
        return await self._page(
            select(NotificationDeliveryAttempt).where(
                NotificationDeliveryAttempt.delivery_id == delivery_id
            ),
            page=page,
            page_size=page_size,
            order_by=(NotificationDeliveryAttempt.attempt_no.desc(),),
        )

    async def get_delivery(self, delivery_id: UUID) -> NotificationDelivery | None:
        return await self._session.get(NotificationDelivery, delivery_id)

    async def lock_delivery(
        self,
        delivery_id: UUID,
    ) -> NotificationDelivery | None:
        return await self._session.scalar(
            select(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id)
            .with_for_update()
        )

    async def lock_event(self, event_id: UUID) -> NotificationEvent | None:
        return await self._session.scalar(
            select(NotificationEvent)
            .where(NotificationEvent.id == event_id)
            .with_for_update()
        )

    async def list_event_deliveries(
        self,
        event_id: UUID,
    ) -> list[NotificationDelivery]:
        result = await self._session.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.event_id == event_id)
            .order_by(NotificationDelivery.channel, NotificationDelivery.generation)
            .execution_options(populate_existing=True)
        )
        return list(result.all())

    def add_delivery(self, delivery: NotificationDelivery) -> None:
        self._session.add(delivery)

    async def flush(self) -> None:
        await self._session.flush()

    async def _page[T](
        self,
        statement: Select[tuple[T]],
        *,
        page: int,
        page_size: int,
        order_by: Sequence[object],
    ) -> AdminPage[T]:
        total = await self._session.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        result = await self._session.scalars(
            statement.order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return AdminPage(
            items=tuple(result.all()),
            page=page,
            page_size=page_size,
            total=int(total or 0),
        )


class NotificationAdminService:
    def __init__(self, repository: NotificationAdminRepository) -> None:
        self._repository = repository

    async def list_events(
        self,
        *,
        page: int,
        page_size: int,
        status: NotificationEventStatus | None = None,
        event_type: str | None = None,
    ) -> AdminPage[NotificationEvent]:
        return await self._repository.list_events(
            page=page,
            page_size=page_size,
            status=status,
            event_type=event_type,
        )

    async def get_event_detail(self, event_id: UUID) -> NotificationEventDetail:
        detail = await self._repository.get_event_detail(event_id)
        if detail is None:
            raise _not_found("notification event")
        return detail

    async def list_deliveries(
        self,
        *,
        page: int,
        page_size: int,
        event_id: UUID | None = None,
        status: NotificationDeliveryStatus | None = None,
        channel: DeliveryChannel | None = None,
    ) -> AdminPage[NotificationDelivery]:
        return await self._repository.list_deliveries(
            page=page,
            page_size=page_size,
            event_id=event_id,
            status=status,
            channel=channel,
        )

    async def list_attempts(
        self,
        delivery_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> AdminPage[NotificationDeliveryAttempt]:
        if await self._repository.get_delivery(delivery_id) is None:
            raise _not_found("notification delivery")
        return await self._repository.list_attempts(
            delivery_id,
            page=page,
            page_size=page_size,
        )

    async def retry_delivery(self, delivery_id: UUID) -> DeliveryMutation:
        delivery = await self._repository.lock_delivery(delivery_id)
        if delivery is None:
            raise _not_found("notification delivery")
        event = await self._repository.lock_event(delivery.event_id)
        if event is None:
            raise _not_found("notification event")
        deliveries = await self._repository.list_event_deliveries(event.id)
        current = _current_delivery(deliveries, DeliveryChannel(delivery.channel))
        if current is None or current.id != delivery.id:
            raise NotificationAdminError(
                code="NOTIFICATION_DELIVERY_RETRY_SUPERSEDED",
                message="a newer delivery generation already exists",
            )
        status = NotificationDeliveryStatus(delivery.status)
        if status is NotificationDeliveryStatus.OUTCOME_UNKNOWN:
            raise NotificationAdminError(
                code="NOTIFICATION_DELIVERY_OUTCOME_UNKNOWN",
                message="an outcome-unknown delivery cannot use ordinary manual retry",
            )
        if status not in _MANUALLY_RETRYABLE_STATUSES:
            raise NotificationAdminError(
                code="NOTIFICATION_DELIVERY_NOT_RETRYABLE",
                message=f"delivery status {status.value} cannot be retried",
            )

        generation = (
            max(
                item.generation
                for item in deliveries
                if DeliveryChannel(item.channel) is DeliveryChannel(delivery.channel)
            )
            + 1
        )
        retried = NotificationDelivery(
            id=uuid4(),
            event_id=delivery.event_id,
            generation=generation,
            channel=delivery.channel,
            config_version=delivery.config_version,
            target_fingerprint=delivery.target_fingerprint,
            status=NotificationDeliveryStatus.PENDING,
            attempt_count=0,
            unknown_compensation_count=0,
            deterministic_message_id=(
                f"notification:{delivery.event_id}:"
                f"{DeliveryChannel(delivery.channel).value}:{generation}"
            ),
        )
        self._repository.add_delivery(retried)
        deliveries.append(retried)
        event.status = aggregate_current_event_status(deliveries)
        await self._repository.flush()
        return DeliveryMutation(delivery=retried, changed=True)

    async def cancel_delivery(self, delivery_id: UUID) -> DeliveryMutation:
        delivery = await self._repository.lock_delivery(delivery_id)
        if delivery is None:
            raise _not_found("notification delivery")
        event = await self._repository.lock_event(delivery.event_id)
        if event is None:
            raise _not_found("notification event")
        deliveries = await self._repository.list_event_deliveries(event.id)
        current = _current_delivery(deliveries, DeliveryChannel(delivery.channel))
        if current is None or current.id != delivery.id:
            raise NotificationAdminError(
                code="NOTIFICATION_DELIVERY_CANCEL_SUPERSEDED",
                message="a newer delivery generation already exists",
            )
        status = NotificationDeliveryStatus(delivery.status)
        if status is NotificationDeliveryStatus.CANCELED:
            return DeliveryMutation(delivery=delivery, changed=False)
        if status not in _CANCELABLE_STATUSES:
            raise NotificationAdminError(
                code="NOTIFICATION_DELIVERY_NOT_CANCELABLE",
                message=f"delivery status {status.value} cannot be canceled",
            )

        delivery.status = NotificationDeliveryStatus.CANCELED
        delivery.next_retry_at = None
        delivery.error_code = "CANCELED_BY_USER"
        event.status = aggregate_current_event_status(deliveries)
        await self._repository.flush()
        return DeliveryMutation(delivery=delivery, changed=True)

    async def retry_failed_batch(
        self,
        delivery_ids: Sequence[UUID],
    ) -> DeliveryRetryBatch:
        if not delivery_ids or len(delivery_ids) > 100:
            raise NotificationAdminError(
                code="NOTIFICATION_DELIVERY_BATCH_SIZE_INVALID",
                message="batch must contain between 1 and 100 delivery ids",
            )
        retried: list[NotificationDelivery] = []
        failures: list[DeliveryRetryFailure] = []
        seen: set[UUID] = set()
        for delivery_id in delivery_ids:
            if delivery_id in seen:
                continue
            seen.add(delivery_id)
            try:
                mutation = await self.retry_delivery(delivery_id)
            except NotificationAdminError as exc:
                failures.append(DeliveryRetryFailure(delivery_id, exc.code))
            else:
                retried.append(mutation.delivery)
        return DeliveryRetryBatch(tuple(retried), tuple(failures))


def aggregate_current_event_status(
    deliveries: Sequence[NotificationDelivery],
) -> str:
    current_by_channel: dict[DeliveryChannel, NotificationDelivery] = {}
    for delivery in deliveries:
        channel = DeliveryChannel(delivery.channel)
        current = current_by_channel.get(channel)
        if current is None or delivery.generation > current.generation:
            current_by_channel[channel] = delivery
    return aggregate_event_status(
        NotificationDeliveryStatus(delivery.status)
        for delivery in current_by_channel.values()
    )


def _current_delivery(
    deliveries: Sequence[NotificationDelivery],
    channel: DeliveryChannel,
) -> NotificationDelivery | None:
    matching = [
        delivery
        for delivery in deliveries
        if DeliveryChannel(delivery.channel) is channel
    ]
    return max(matching, key=lambda item: item.generation, default=None)


def _validate_page(page: int, page_size: int) -> None:
    if page < 1 or not 1 <= page_size <= 200:
        raise ValueError(
            "page must be positive and page_size must be between 1 and 200"
        )


def _not_found(resource: str) -> NotificationAdminError:
    return NotificationAdminError(
        code="NOTIFICATION_RESOURCE_NOT_FOUND",
        message=f"{resource} was not found",
    )
