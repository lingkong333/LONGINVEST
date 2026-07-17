from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.monitoring.models import (
    MonitorSubscription,
    MonitorSubscriptionRevision,
)


class MonitorSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def security_lock_statement(security_id):
        return select(
            func.pg_advisory_xact_lock(
                func.hashtext(f"monitor-subscription:{security_id}")
            )
        )

    @staticmethod
    def transition_statement():
        return update(MonitorSubscription).where(
            MonitorSubscription.version == 1, MonitorSubscription.status == "PAUSED"
        )

    @staticmethod
    def enabled_schedule_statement():
        return (
            select(MonitorSubscription, MonitorSubscriptionRevision)
            .join(
                MonitorSubscriptionRevision,
                MonitorSubscription.current_revision_id
                == MonitorSubscriptionRevision.id,
            )
            .where(
                MonitorSubscription.status == "ENABLED",
                MonitorSubscription.archived_at.is_(None),
                MonitorSubscriptionRevision.schedule_id.is_not(None),
            )
        )

    async def lock_security(self, security_id: UUID) -> None:
        await self.session.execute(self.security_lock_statement(str(security_id)))

    async def list(self, *, include_archived=False):
        stmt = select(MonitorSubscription)
        if not include_archived:
            stmt = stmt.where(MonitorSubscription.archived_at.is_(None))
        return list(
            (
                await self.session.scalars(
                    stmt.order_by(
                        MonitorSubscription.created_at, MonitorSubscription.id
                    )
                )
            ).all()
        )

    async def find_open_by_security(self, security_id: UUID):
        return await self.session.scalar(
            select(MonitorSubscription).where(
                MonitorSubscription.security_id == security_id,
                MonitorSubscription.archived_at.is_(None),
            )
        )

    async def get(self, subscription_id: UUID, *, for_update=False):
        stmt = select(MonitorSubscription).where(
            MonitorSubscription.id == subscription_id
        )
        return await self.session.scalar(stmt.with_for_update() if for_update else stmt)

    async def get_revision(self, subscription_id: UUID, revision_id: UUID):
        return await self.session.scalar(
            select(MonitorSubscriptionRevision).where(
                MonitorSubscriptionRevision.subscription_id == subscription_id,
                MonitorSubscriptionRevision.id == revision_id,
            )
        )

    async def list_revisions(self, subscription_id: UUID):
        return list(
            (
                await self.session.scalars(
                    select(MonitorSubscriptionRevision)
                    .where(
                        MonitorSubscriptionRevision.subscription_id == subscription_id
                    )
                    .order_by(MonitorSubscriptionRevision.revision_no.desc())
                )
            ).all()
        )

    async def enabled_schedule_rows(self):
        return list(
            (await self.session.execute(self.enabled_schedule_statement())).all()
        )

    async def create(self, owner) -> None:
        self.session.add(owner)
        await self.session.flush()

    async def add_revision(self, revision) -> None:
        self.session.add(revision)
        await self.session.flush()

    async def initialize_current(self, subscription_id, revision_id) -> None:
        await self.session.execute(
            update(MonitorSubscription)
            .where(MonitorSubscription.id == subscription_id)
            .values(current_revision_id=revision_id)
        )

    async def switch_revision(self, subscription_id, *, revision_id, expected_version):
        changed = await self.session.scalar(
            update(MonitorSubscription)
            .where(
                MonitorSubscription.id == subscription_id,
                MonitorSubscription.version == expected_version,
                MonitorSubscription.archived_at.is_(None),
            )
            .values(current_revision_id=revision_id, version=expected_version + 1)
            .returning(MonitorSubscription.id)
        )
        return changed is not None

    async def transition(
        self,
        subscription_id,
        *,
        expected_status,
        expected_version,
        status,
        archived_at=None,
    ):
        changed = await self.session.scalar(
            update(MonitorSubscription)
            .where(
                MonitorSubscription.id == subscription_id,
                MonitorSubscription.status == str(expected_status),
                MonitorSubscription.version == expected_version,
            )
            .values(
                status=str(status),
                version=expected_version + 1,
                archived_at=archived_at,
            )
            .returning(MonitorSubscription.id)
        )
        return changed is not None
