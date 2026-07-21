from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.targets.models import (
    SubscriptionTargetBinding,
    TargetRevision,
)


class TargetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_binding(
        self, subscription_id: UUID
    ) -> SubscriptionTargetBinding | None:
        return await self._session.scalar(
            select(SubscriptionTargetBinding)
            .where(SubscriptionTargetBinding.subscription_id == subscription_id)
            .with_for_update()
        )

    async def create_binding(
        self, subscription_id: UUID
    ) -> SubscriptionTargetBinding:
        binding = SubscriptionTargetBinding(
            id=uuid4(),
            subscription_id=subscription_id,
            current_revision_id=None,
            status="MISSING",
            version=1,
        )
        self._session.add(binding)
        await self._session.flush()
        return binding

    async def find_revision_by_idempotency(
        self, subscription_id: UUID, key: str
    ) -> TargetRevision | None:
        return await self._session.scalar(
            select(TargetRevision).where(
                TargetRevision.subscription_id == subscription_id,
                TargetRevision.idempotency_key == key,
            )
        )

    async def get_revision(self, revision_id: UUID) -> TargetRevision | None:
        return await self._session.scalar(
            select(TargetRevision).where(TargetRevision.id == revision_id)
        )

    async def list_revisions(
        self, subscription_id: UUID
    ) -> tuple[TargetRevision, ...]:
        rows = await self._session.scalars(
            select(TargetRevision)
            .where(TargetRevision.subscription_id == subscription_id)
            .order_by(TargetRevision.revision_no.desc(), TargetRevision.id.desc())
        )
        return tuple(rows.all())

    async def persist_revision(self, revision: TargetRevision) -> None:
        self._session.add(revision)

    async def flush(self) -> None:
        await self._session.flush()
