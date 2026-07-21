from dataclasses import dataclass
from typing import Any

from long_invest.platform.outbox.service import TransactionalOutboxWriter


@dataclass(frozen=True, slots=True)
class SignalDomainEvent:
    event_type: str
    aggregate_id: str
    dedupe_key: str
    payload: dict[str, Any]


class SignalOutbox:
    def __init__(self, session: Any, writer: Any | None = None) -> None:
        self._session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def append(self, event: SignalDomainEvent) -> None:
        await self._writer.append(
            session=self._session,
            topic=event.event_type,
            aggregate_type="signal",
            aggregate_id=event.aggregate_id,
            queue="domain-events",
            payload={"event_type": event.event_type, **event.payload},
            dedupe_key=event.dedupe_key,
        )
