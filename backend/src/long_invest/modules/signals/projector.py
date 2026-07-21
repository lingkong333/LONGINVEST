from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus

SUPPORTED_SIGNAL_EVENT_TOPICS = frozenset(
    {
        "quote_cycle.finalized",
        "target.activated",
        "position.became_holding",
    }
)


@dataclass(frozen=True, slots=True)
class SignalProjectionEvent:
    id: UUID
    topic: str
    aggregate_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class SignalProjectionReport:
    claimed: int
    projected: int


class SignalProjectionStore(Protocol):
    async def claim_supported(
        self,
        *,
        limit: int,
    ) -> tuple[SignalProjectionEvent, ...]: ...

    async def mark_dispatched(
        self,
        event_id: UUID,
        *,
        dispatched_at: datetime,
    ) -> None: ...


class SignalProjectionRepository:
    """Transaction-bound access to signal-owned domain-event projections."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._claimed: dict[UUID, EventOutbox] = {}

    @staticmethod
    def claim_statement(*, limit: int, now: datetime) -> Select[tuple[EventOutbox]]:
        return (
            select(EventOutbox)
            .where(
                EventOutbox.topic.in_(SUPPORTED_SIGNAL_EVENT_TOPICS),
                EventOutbox.status == OutboxStatus.PENDING,
                EventOutbox.next_attempt_at <= now,
            )
            .order_by(EventOutbox.created_at, EventOutbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def claim_supported(
        self,
        *,
        limit: int,
    ) -> tuple[SignalProjectionEvent, ...]:
        if limit < 1:
            raise ValueError("projection limit must be positive")
        rows = (
            await self._session.scalars(
                self.claim_statement(limit=limit, now=datetime.now(UTC))
            )
        ).all()
        self._claimed = {row.id: row for row in rows}
        return tuple(
            SignalProjectionEvent(
                id=row.id,
                topic=row.topic,
                aggregate_id=row.aggregate_id,
                payload=row.payload,
            )
            for row in rows
        )

    async def mark_dispatched(
        self,
        event_id: UUID,
        *,
        dispatched_at: datetime,
    ) -> None:
        event = self._claimed.get(event_id)
        if event is None or event.status != OutboxStatus.PENDING:
            raise RuntimeError("signal projection event is not claimed")
        event.status = OutboxStatus.DISPATCHED
        event.dispatched_at = dispatched_at
        event.locked_at = None
        event.locked_by = None
        event.last_error_code = None
        event.last_error_summary = None
        await self._session.flush()


class SignalEventProjector:
    def __init__(
        self,
        database: Any,
        *,
        repository_factory: Callable[[Any], SignalProjectionStore] = (
            SignalProjectionRepository
        ),
        job_service_factory: Callable[[Any], Any] = JobService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._repository_factory = repository_factory
        self._job_service_factory = job_service_factory
        self._clock = clock

    async def project_once(self, *, limit: int = 100) -> SignalProjectionReport:
        if limit < 1:
            raise ValueError("projection limit must be positive")
        async with self._database.transaction() as session:
            repository = self._repository_factory(session)
            events = await repository.claim_supported(limit=limit)
            jobs = self._job_service_factory(session)
            for event in events:
                await jobs.submit(_job_command(event))
                await repository.mark_dispatched(
                    event.id,
                    dispatched_at=self._clock(),
                )
        count = len(events)
        return SignalProjectionReport(claimed=count, projected=count)


def _job_command(event: SignalProjectionEvent) -> SubmitJob:
    source = {
        "source_event_id": str(event.id),
    }
    request_id = _optional_str(event.payload, "request_id") or (
        f"signal-projector:{event.id}"
    )
    common = {
        "queue": "signals",
        "idempotency_scope": "signal-event-projector",
        "idempotency_key": f"{event.topic}:{event.id}",
        "request_id": request_id,
    }
    if event.topic == "quote_cycle.finalized":
        item_ids = _string_list(event.payload, "valid_item_ids")
        cycle_id = _required_str(event.payload, "cycle_id")
        return SubmitJob(
            job_type="SIGNAL_EVALUATE_BATCH",
            config_snapshot={
                **source,
                "reason": "QUOTE_FINALIZED",
                "quote_cycle_id": cycle_id,
                "eligible_item_ids": item_ids,
            },
            business_object_type="quote_cycle",
            business_object_id=cycle_id,
            soft_timeout_seconds=240,
            hard_timeout_seconds=300,
            **common,
        )
    if event.topic == "target.activated":
        subscription_id = _required_str(event.payload, "subscription_id")
        return SubmitJob(
            job_type="SIGNAL_REEVALUATE",
            config_snapshot={
                **source,
                "reason": "TARGET_ACTIVATED",
                "subscription_id": subscription_id,
                "target_revision_id": _required_str(event.payload, "revision_id"),
                "target_binding_version": _positive_int(
                    event.payload,
                    "binding_version",
                ),
            },
            business_object_type="monitor_subscription",
            business_object_id=subscription_id,
            soft_timeout_seconds=30,
            hard_timeout_seconds=60,
            **common,
        )
    if event.topic == "position.became_holding":
        security_id = _required_str(event.payload, "security_id")
        return SubmitJob(
            job_type="SIGNAL_REEVALUATE",
            config_snapshot={
                **source,
                "reason": "POSITION_BECAME_HOLDING",
                "security_id": security_id,
                "symbol": _required_str(event.payload, "symbol"),
                "position_version": _positive_int(
                    event.payload,
                    "position_version",
                ),
            },
            business_object_type="security",
            business_object_id=security_id,
            soft_timeout_seconds=30,
            hard_timeout_seconds=60,
            **common,
        )
    raise ValueError(f"unsupported signal projection topic: {event.topic}")


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _positive_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _string_list(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return list(value)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value
