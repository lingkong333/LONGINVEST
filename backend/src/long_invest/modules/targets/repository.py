from uuid import UUID, uuid4

from sqlalchemy import func, select
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

    async def get_binding(
        self, subscription_id: UUID
    ) -> SubscriptionTargetBinding | None:
        return await self._session.scalar(
            select(SubscriptionTargetBinding).where(
                SubscriptionTargetBinding.subscription_id == subscription_id
            )
        )

    async def list_current_rows(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[tuple[SubscriptionTargetBinding, TargetRevision], ...]:
        _validate_page(page, page_size)
        rows = await self._session.execute(
            select(SubscriptionTargetBinding, TargetRevision)
            .join(
                TargetRevision,
                TargetRevision.id == SubscriptionTargetBinding.current_revision_id,
            )
            .where(
                SubscriptionTargetBinding.current_revision_id.is_not(None),
                SubscriptionTargetBinding.activated_at.is_not(None),
            )
            .order_by(
                SubscriptionTargetBinding.activated_at.desc(),
                SubscriptionTargetBinding.id.desc(),
            )
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return tuple((binding, revision) for binding, revision in rows.all())

    async def count_bindings(self) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(SubscriptionTargetBinding)
            .join(
                TargetRevision,
                TargetRevision.id == SubscriptionTargetBinding.current_revision_id,
            )
            .where(
                SubscriptionTargetBinding.current_revision_id.is_not(None),
                SubscriptionTargetBinding.activated_at.is_not(None),
            )
        )
        return int(total or 0)

    async def create_binding(self, subscription_id: UUID) -> SubscriptionTargetBinding:
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
        self,
        subscription_id: UUID,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[TargetRevision, ...]:
        _validate_page(page, page_size)
        rows = await self._session.scalars(
            select(TargetRevision)
            .where(TargetRevision.subscription_id == subscription_id)
            .order_by(TargetRevision.created_at.desc(), TargetRevision.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return tuple(rows.all())

    async def count_revisions(self, subscription_id: UUID) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(TargetRevision)
            .where(TargetRevision.subscription_id == subscription_id)
        )
        return int(total or 0)

    async def next_revision_no(self, subscription_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(TargetRevision.revision_no)).where(
                TargetRevision.subscription_id == subscription_id
            )
        )
        return int(current or 0) + 1

    async def persist_revision(self, revision: TargetRevision) -> None:
        self._session.add(revision)

    async def flush(self) -> None:
        await self._session.flush()


def _validate_page(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 200:
        raise ValueError("pagination is outside the supported range")
