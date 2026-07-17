from uuid import UUID

from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.positions.models import UserPosition, UserPositionHistory


class PositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_current(self, security_id: UUID) -> UserPosition | None:
        return await self._session.scalar(
            select(UserPosition).where(UserPosition.security_id == security_id)
        )

    async def lock_current(self, security_id: UUID) -> UserPosition | None:
        return await self._session.scalar(
            select(UserPosition)
            .where(UserPosition.security_id == security_id)
            .with_for_update()
        )

    async def lock_security(self, security_id: UUID) -> None:
        await self._session.scalar(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended(str(security_id), 0))
            )
        )

    async def list_current(self) -> list[UserPosition]:
        rows = await self._session.scalars(
            select(UserPosition).order_by(UserPosition.symbol)
        )
        return list(rows.all())

    async def list_history(
        self, security_id: UUID | None = None
    ) -> list[UserPositionHistory]:
        statement = select(UserPositionHistory)
        if security_id is not None:
            statement = statement.where(UserPositionHistory.security_id == security_id)
        rows = await self._session.scalars(
            statement.order_by(UserPositionHistory.created_at.desc())
        )
        return list(rows.all())

    async def find_history_by_idempotency(
        self, security_id: UUID, idempotency_key: str
    ) -> UserPositionHistory | None:
        return await self._session.scalar(
            select(UserPositionHistory).where(
                UserPositionHistory.security_id == security_id,
                UserPositionHistory.idempotency_key == idempotency_key,
            )
        )

    async def add_change(
        self, position: UserPosition, history: UserPositionHistory
    ) -> None:
        latest_history_id = history.id
        if not inspect(position).persistent:
            position.latest_history_id = None
            self._session.add(position)
            await self._session.flush()
        self._session.add(history)
        await self._session.flush()
        position.latest_history_id = latest_history_id
        await self._session.flush()
