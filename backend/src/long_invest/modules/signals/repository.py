from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.signals.models import (
    SignalEvaluation,
    SignalEvent,
    SignalState,
)


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_evaluation_by_idempotency(
        self, subscription_id: UUID, idempotency_key: str
    ) -> SignalEvaluation | None:
        return await self._session.scalar(
            select(SignalEvaluation).where(
                SignalEvaluation.subscription_id == subscription_id,
                SignalEvaluation.idempotency_key == idempotency_key,
            )
        )

    async def lock_or_create_state(self, subscription_id: UUID) -> SignalState:
        await self._session.execute(
            insert(SignalState)
            .values(subscription_id=subscription_id, zone="UNKNOWN", version=1)
            .on_conflict_do_nothing(index_elements=[SignalState.subscription_id])
        )
        state = await self._session.scalar(
            select(SignalState)
            .where(SignalState.subscription_id == subscription_id)
            .with_for_update()
        )
        if state is None:
            raise RuntimeError("signal state initialization failed")
        return state

    async def get_state(self, subscription_id: UUID) -> SignalState | None:
        return await self._session.scalar(
            select(SignalState).where(SignalState.subscription_id == subscription_id)
        )

    async def lock_state(self, subscription_id: UUID) -> SignalState | None:
        return await self._session.scalar(
            select(SignalState)
            .where(SignalState.subscription_id == subscription_id)
            .with_for_update()
        )

    async def get_evaluation(self, evaluation_id: UUID) -> SignalEvaluation | None:
        return await self._session.get(SignalEvaluation, evaluation_id)

    async def get_event(self, event_id: UUID) -> SignalEvent | None:
        return await self._session.get(SignalEvent, event_id)

    async def get_event_by_evaluation(self, evaluation_id: UUID) -> SignalEvent | None:
        return await self._session.scalar(
            select(SignalEvent).where(SignalEvent.evaluation_id == evaluation_id)
        )

    async def add_evaluation(self, evaluation: SignalEvaluation) -> None:
        self._session.add(evaluation)

    async def add_event(self, event: SignalEvent) -> None:
        self._session.add(event)

    async def list_states(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[SignalState, ...]:
        _validate_page(page, page_size)
        rows = await self._session.scalars(
            select(SignalState)
            .order_by(SignalState.updated_at.desc(), SignalState.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return tuple(rows.all())

    async def count_states(self) -> int:
        return int(await self._session.scalar(select(func.count(SignalState.id))) or 0)

    async def list_evaluations(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[SignalEvaluation, ...]:
        _validate_page(page, page_size)
        rows = await self._session.scalars(
            select(SignalEvaluation)
            .order_by(SignalEvaluation.created_at.desc(), SignalEvaluation.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return tuple(rows.all())

    async def count_evaluations(self) -> int:
        return int(
            await self._session.scalar(select(func.count(SignalEvaluation.id))) or 0
        )

    async def list_events(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[SignalEvent, ...]:
        _validate_page(page, page_size)
        rows = await self._session.scalars(
            select(SignalEvent)
            .order_by(SignalEvent.created_at.desc(), SignalEvent.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return tuple(rows.all())

    async def count_events(self) -> int:
        return int(await self._session.scalar(select(func.count(SignalEvent.id))) or 0)

    async def flush(self) -> None:
        await self._session.flush()


def _validate_page(page: int, page_size: int) -> None:
    if page < 1 or not 1 <= page_size <= 200:
        raise ValueError("invalid pagination")
