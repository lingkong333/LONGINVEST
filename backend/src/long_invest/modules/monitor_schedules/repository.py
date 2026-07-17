from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.monitor_schedules.models import (
    MonitorSchedule,
    MonitorScheduleRevision,
)


class MonitorScheduleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def lock_statement():
        return (
            select(MonitorSchedule)
            .where(MonitorSchedule.id.is_not(None))
            .with_for_update()
        )

    @staticmethod
    def switch_statement():
        return update(MonitorSchedule).where(MonitorSchedule.version == 1)

    @staticmethod
    def archive_statement():
        return update(MonitorSchedule).where(
            MonitorSchedule.version == 1, MonitorSchedule.archived_at.is_(None)
        )

    @staticmethod
    def idempotency_lock_statement(idempotency_key: str):
        scope = f"monitor-schedule:create:{idempotency_key}"
        return select(func.pg_advisory_xact_lock(func.hashtext(scope)))

    async def lock_idempotency(self, idempotency_key: str) -> None:
        await self.session.execute(self.idempotency_lock_statement(idempotency_key))

    async def list(self, *, include_archived: bool = False) -> list[MonitorSchedule]:
        statement = select(MonitorSchedule)
        if not include_archived:
            statement = statement.where(MonitorSchedule.archived_at.is_(None))
        rows = await self.session.scalars(
            statement.order_by(MonitorSchedule.created_at, MonitorSchedule.id)
        )
        return list(rows.all())

    async def get(
        self, schedule_id: UUID, *, for_update: bool = False
    ) -> MonitorSchedule | None:
        statement = select(MonitorSchedule).where(MonitorSchedule.id == schedule_id)
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def find_replay(
        self, schedule_id: UUID | None, idempotency_key: str
    ) -> MonitorScheduleRevision | None:
        statement = select(MonitorScheduleRevision).where(
            MonitorScheduleRevision.idempotency_key == idempotency_key
        )
        if schedule_id is not None:
            statement = statement.where(
                MonitorScheduleRevision.schedule_id == schedule_id
            )
        return await self.session.scalar(
            statement.order_by(MonitorScheduleRevision.created_at).limit(1)
        )

    async def get_revision(
        self, schedule_id: UUID, revision_id: UUID
    ) -> MonitorScheduleRevision | None:
        return await self.session.scalar(
            select(MonitorScheduleRevision).where(
                MonitorScheduleRevision.schedule_id == schedule_id,
                MonitorScheduleRevision.id == revision_id,
            )
        )

    async def list_revisions(self, schedule_id: UUID) -> list[MonitorScheduleRevision]:
        rows = await self.session.scalars(
            select(MonitorScheduleRevision)
            .where(MonitorScheduleRevision.schedule_id == schedule_id)
            .order_by(MonitorScheduleRevision.revision_no.desc())
        )
        return list(rows.all())

    async def create_schedule(self, schedule: MonitorSchedule) -> None:
        self.session.add(schedule)
        await self.session.flush()

    async def add_revision(self, revision: MonitorScheduleRevision) -> None:
        self.session.add(revision)
        await self.session.flush()

    async def initialize_current(self, schedule_id: UUID, revision_id: UUID) -> None:
        await self.session.execute(
            update(MonitorSchedule)
            .where(MonitorSchedule.id == schedule_id)
            .values(current_revision_id=revision_id)
        )

    async def switch_current(
        self, schedule_id: UUID, *, revision_id: UUID, name: str, expected_version: int
    ) -> bool:
        changed = await self.session.scalar(
            update(MonitorSchedule)
            .where(
                MonitorSchedule.id == schedule_id,
                MonitorSchedule.version == expected_version,
                MonitorSchedule.archived_at.is_(None),
            )
            .values(
                current_revision_id=revision_id, name=name, version=expected_version + 1
            )
            .returning(MonitorSchedule.id)
        )
        return changed is not None

    async def archive(
        self, schedule_id: UUID, *, expected_version: int, archived_at: datetime
    ) -> bool:
        changed = await self.session.scalar(
            update(MonitorSchedule)
            .where(
                MonitorSchedule.id == schedule_id,
                MonitorSchedule.version == expected_version,
                MonitorSchedule.archived_at.is_(None),
            )
            .values(archived_at=archived_at, version=expected_version + 1)
            .returning(MonitorSchedule.id)
        )
        return changed is not None
