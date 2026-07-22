from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.alerts.contracts import AlertStatus
from long_invest.modules.alerts.models import (
    SystemAlert,
    SystemAlertAction,
    SystemAlertOccurrence,
)


class AlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_aggregation_key(self, key: str) -> None:
        await self.session.scalar(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )

    async def find_by_key(self, key: str, *, lock: bool = False) -> SystemAlert | None:
        statement = select(SystemAlert).where(SystemAlert.aggregation_key == key)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def get(self, alert_id: UUID, *, lock: bool = False) -> SystemAlert | None:
        statement = select(SystemAlert).where(SystemAlert.id == alert_id)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def occurrence_by_source(
        self, source_event_id: str
    ) -> SystemAlertOccurrence | None:
        return await self.session.scalar(
            select(SystemAlertOccurrence).where(
                SystemAlertOccurrence.source_event_id == source_event_id
            )
        )

    async def action_by_idempotency(self, key: str) -> SystemAlertAction | None:
        return await self.session.scalar(
            select(SystemAlertAction).where(SystemAlertAction.idempotency_key == key)
        )

    async def unresolved(self) -> tuple[SystemAlert, ...]:
        rows = await self.session.scalars(
            select(SystemAlert)
            .where(SystemAlert.status != AlertStatus.RESOLVED)
            .order_by(SystemAlert.last_seen_at, SystemAlert.id)
        )
        return tuple(rows.all())

    async def list_alerts(
        self, *, status=None, severity=None, alert_type=None, page=1, page_size=50
    ):
        statement = select(SystemAlert)
        if status is not None:
            statement = statement.where(SystemAlert.status == status)
        if severity is not None:
            statement = statement.where(SystemAlert.severity == severity)
        if alert_type is not None:
            statement = statement.where(SystemAlert.alert_type == alert_type)
        total = await self.session.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        rows = await self.session.scalars(
            statement.order_by(SystemAlert.last_seen_at.desc(), SystemAlert.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.all()), int(total or 0)

    async def occurrences(self, alert_id: UUID, *, page=1, page_size=50):
        statement = select(SystemAlertOccurrence).where(
            SystemAlertOccurrence.alert_id == alert_id
        )
        total = await self.session.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        rows = await self.session.scalars(
            statement.order_by(SystemAlertOccurrence.occurred_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.all()), int(total or 0)

    async def actions(self, alert_id: UUID, *, page=1, page_size=50):
        statement = select(SystemAlertAction).where(
            SystemAlertAction.alert_id == alert_id
        )
        total = await self.session.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        rows = await self.session.scalars(
            statement.order_by(SystemAlertAction.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.all()), int(total or 0)

    def add_all(self, *items) -> None:
        self.session.add_all(items)

    async def flush(self) -> None:
        await self.session.flush()
