from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.targets.models import (
    SubscriptionTargetBinding,
    TargetCalculationRun,
    TargetReview,
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

    async def get_calculation_by_idempotency(
        self, subscription_id: UUID, key: str
    ) -> TargetCalculationRun | None:
        return await self._session.scalar(
            select(TargetCalculationRun).where(
                TargetCalculationRun.subscription_id == subscription_id,
                TargetCalculationRun.idempotency_key == key,
            )
        )

    async def get_calculation(
        self, run_id: UUID, *, for_update: bool = False
    ) -> TargetCalculationRun | None:
        query = select(TargetCalculationRun).where(TargetCalculationRun.id == run_id)
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def get_latest_calculation(
        self, subscription_id: UUID, *, for_update: bool = False
    ) -> TargetCalculationRun | None:
        query = (
            select(TargetCalculationRun)
            .where(TargetCalculationRun.subscription_id == subscription_id)
            .order_by(
                TargetCalculationRun.created_at.desc(), TargetCalculationRun.id.desc()
            )
            .limit(1)
        )
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def persist_calculation(self, run: TargetCalculationRun) -> None:
        self._session.add(run)

    async def list_calculations(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[TargetCalculationRun, ...]:
        _validate_page(page, page_size)
        rows = await self._session.scalars(
            select(TargetCalculationRun)
            .order_by(
                TargetCalculationRun.created_at.desc(), TargetCalculationRun.id.desc()
            )
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return tuple(rows.all())

    async def count_calculations(self) -> int:
        total = await self._session.scalar(
            select(func.count()).select_from(TargetCalculationRun)
        )
        return int(total or 0)

    async def get_review(
        self, review_id: UUID, *, for_update: bool = False
    ) -> TargetReview | None:
        query = select(TargetReview).where(TargetReview.id == review_id)
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def get_review_by_candidate(self, revision_id: UUID) -> TargetReview | None:
        return await self._session.scalar(
            select(TargetReview).where(
                TargetReview.candidate_revision_id == revision_id
            )
        )

    async def list_pending_reviews_for_subscription(
        self, subscription_id: UUID
    ) -> tuple[TargetReview, ...]:
        rows = await self._session.scalars(
            select(TargetReview)
            .join(
                TargetRevision,
                TargetRevision.id == TargetReview.candidate_revision_id,
            )
            .where(
                TargetRevision.subscription_id == subscription_id,
                TargetReview.status == "PENDING",
            )
            .with_for_update()
        )
        return tuple(rows.all())

    async def persist_review(self, review: TargetReview) -> None:
        self._session.add(review)

    async def list_reviews(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[TargetReview, ...]:
        _validate_page(page, page_size)
        rows = await self._session.scalars(
            select(TargetReview)
            .order_by(TargetReview.created_at.desc(), TargetReview.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return tuple(rows.all())

    async def count_reviews(self) -> int:
        total = await self._session.scalar(
            select(func.count()).select_from(TargetReview)
        )
        return int(total or 0)

    async def flush(self) -> None:
        await self._session.flush()


def _validate_page(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 200:
        raise ValueError("pagination is outside the supported range")
