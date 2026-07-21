from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.strategies.models import (
    Strategy,
    StrategyDraft,
    StrategyDraftRevision,
    StrategyRun,
    StrategyValidationRun,
    StrategyVersion,
)


class StrategyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def strategy_statement(strategy_id: UUID, *, for_update: bool = False):
        statement = select(Strategy).where(Strategy.id == strategy_id)
        return statement.with_for_update() if for_update else statement

    @staticmethod
    def list_statement(*, page: int, page_size: int, include_archived: bool):
        statement = select(Strategy)
        if not include_archived:
            statement = statement.where(Strategy.status != "ARCHIVED")
        return (
            statement.order_by(Strategy.name, Strategy.id)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )

    @staticmethod
    def update_draft_statement(
        strategy_id: UUID, *, source_code: str, expected_version: int
    ):
        return (
            update(StrategyDraft)
            .where(
                StrategyDraft.strategy_id == strategy_id,
                StrategyDraft.draft_version == expected_version,
            )
            .values(
                source_code=source_code,
                draft_version=StrategyDraft.draft_version + 1,
            )
            .returning(StrategyDraft)
        )

    async def list_strategies(
        self, *, page: int, page_size: int, include_archived: bool
    ) -> tuple[list[Strategy], int]:
        condition = True if include_archived else Strategy.status != "ARCHIVED"
        total = int(
            await self.session.scalar(
                select(func.count()).select_from(Strategy).where(condition)
            )
            or 0
        )
        rows = await self.session.scalars(
            self.list_statement(
                page=page, page_size=page_size, include_archived=include_archived
            )
        )
        return list(rows.all()), total

    async def get_strategy(
        self, strategy_id: UUID, *, for_update: bool = False
    ) -> Strategy | None:
        return await self.session.scalar(
            self.strategy_statement(strategy_id, for_update=for_update)
        )

    async def get_draft(
        self, strategy_id: UUID, *, for_update: bool = False
    ) -> StrategyDraft | None:
        statement = select(StrategyDraft).where(
            StrategyDraft.strategy_id == strategy_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def create_strategy(
        self, strategy: Strategy, draft: StrategyDraft
    ) -> None:
        self.session.add_all((strategy, draft))
        await self.session.flush()

    async def update_draft(
        self, strategy_id: UUID, *, source_code: str, expected_version: int
    ) -> StrategyDraft | None:
        return await self.session.scalar(
            self.update_draft_statement(
                strategy_id,
                source_code=source_code,
                expected_version=expected_version,
            )
        )

    async def add_revision(self, revision: StrategyDraftRevision) -> None:
        self.session.add(revision)
        await self.session.flush()

    async def next_revision_no(self, draft_id: UUID) -> int:
        current = await self.session.scalar(
            select(func.max(StrategyDraftRevision.revision_no)).where(
                StrategyDraftRevision.draft_id == draft_id
            )
        )
        return int(current or 0) + 1

    async def get_revision(
        self, strategy_id: UUID, revision_id: UUID
    ) -> StrategyDraftRevision | None:
        return await self.session.scalar(
            select(StrategyDraftRevision)
            .join(StrategyDraft, StrategyDraft.id == StrategyDraftRevision.draft_id)
            .where(
                StrategyDraft.strategy_id == strategy_id,
                StrategyDraftRevision.id == revision_id,
            )
        )

    async def list_revisions(
        self, strategy_id: UUID, *, page: int, page_size: int
    ) -> tuple[list[StrategyDraftRevision], int]:
        condition = StrategyDraft.strategy_id == strategy_id
        total = int(
            await self.session.scalar(
                select(func.count())
                .select_from(StrategyDraftRevision)
                .join(StrategyDraft)
                .where(condition)
            )
            or 0
        )
        rows = await self.session.scalars(
            select(StrategyDraftRevision)
            .join(StrategyDraft)
            .where(condition)
            .order_by(StrategyDraftRevision.revision_no.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return list(rows.all()), total

    async def set_strategy_status(self, strategy_id: UUID, status: str) -> None:
        await self.session.execute(
            update(Strategy).where(Strategy.id == strategy_id).values(status=status)
        )

    async def set_strategy_name(self, strategy_id: UUID, name: str) -> None:
        await self.session.execute(
            update(Strategy).where(Strategy.id == strategy_id).values(name=name)
        )

    async def rename_strategy(
        self, strategy_id: UUID, *, name: str, expected_version: int
    ) -> StrategyDraft | None:
        draft = await self.session.scalar(
            update(StrategyDraft)
            .where(
                StrategyDraft.strategy_id == strategy_id,
                StrategyDraft.draft_version == expected_version,
            )
            .values(draft_version=StrategyDraft.draft_version + 1)
            .returning(StrategyDraft)
        )
        if draft is None:
            return None
        await self.set_strategy_name(strategy_id, name)
        return draft

    async def get_validation_run(
        self, validation_run_id: UUID, *, for_update: bool = False
    ) -> StrategyValidationRun | None:
        statement = select(StrategyValidationRun).where(
            StrategyValidationRun.id == validation_run_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def add_validation_run(self, run: StrategyValidationRun) -> None:
        self.session.add(run)
        await self.session.flush()

    async def complete_validation_run(
        self,
        validation_run_id: UUID,
        *,
        status: str,
        error_code: str | None,
        evidence_snapshot: dict,
        completed_at,
    ) -> StrategyValidationRun | None:
        return await self.session.scalar(
            update(StrategyValidationRun)
            .where(
                StrategyValidationRun.id == validation_run_id,
                StrategyValidationRun.status.in_(("PENDING", "RUNNING")),
            )
            .values(
                status=status,
                error_code=error_code,
                evidence_snapshot=evidence_snapshot,
                completed_at=completed_at,
            )
            .returning(StrategyValidationRun)
        )

    async def bind_validation_run(
        self,
        validation_run_id: UUID,
        version_id: UUID,
        *,
        strategy_id: UUID,
        draft_version: int,
        source_code_hash: str,
    ) -> bool:
        changed = await self.session.scalar(
            update(StrategyValidationRun)
            .where(
                StrategyValidationRun.id == validation_run_id,
                StrategyValidationRun.strategy_id == strategy_id,
                StrategyValidationRun.draft_version == draft_version,
                StrategyValidationRun.source_code_hash == source_code_hash,
                StrategyValidationRun.status == "SUCCEEDED",
                StrategyValidationRun.strategy_version_id.is_(None),
            )
            .values(strategy_version_id=version_id)
            .returning(StrategyValidationRun.id)
        )
        return changed is not None

    async def next_version_no(self, strategy_id: UUID) -> int:
        current = await self.session.scalar(
            select(func.max(StrategyVersion.version_no)).where(
                StrategyVersion.strategy_id == strategy_id
            )
        )
        return int(current or 0) + 1

    async def add_version(self, version: StrategyVersion) -> None:
        self.session.add(version)
        await self.session.flush()

    async def add_strategy_run(self, run: StrategyRun) -> None:
        self.session.add(run)
        await self.session.flush()

    async def get_strategy_run(
        self, run_id: UUID, *, for_update: bool = False
    ) -> StrategyRun | None:
        statement = select(StrategyRun).where(StrategyRun.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def get_publish_run_for_version(
        self, version_id: UUID, *, for_update: bool = False
    ) -> StrategyRun | None:
        statement = select(StrategyRun).where(
            StrategyRun.strategy_version_id == version_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def set_strategy_run_status(self, run_id: UUID, status: str) -> None:
        await self.session.execute(
            update(StrategyRun).where(StrategyRun.id == run_id).values(status=status)
        )

    async def list_recoverable_publish_runs(self) -> list[StrategyRun]:
        rows = await self.session.scalars(
            select(StrategyRun)
            .join(
                StrategyVersion,
                StrategyVersion.id == StrategyRun.strategy_version_id,
            )
            .where(
                StrategyRun.status.in_(("PENDING", "RUNNING", "FAILED")),
                StrategyVersion.status.in_(("PUBLISHING", "PUBLISH_FAILED")),
            )
            .order_by(StrategyRun.id)
        )
        return list(rows.all())

    async def latest_published_version(
        self, strategy_id: UUID
    ) -> StrategyVersion | None:
        return await self.session.scalar(
            select(StrategyVersion)
            .where(
                StrategyVersion.strategy_id == strategy_id,
                StrategyVersion.status.in_(("PUBLISHED", "ARCHIVED")),
            )
            .order_by(StrategyVersion.version_no.desc())
            .limit(1)
        )

    async def get_version(
        self, strategy_id: UUID, version_id: UUID, *, for_update: bool = False
    ) -> StrategyVersion | None:
        statement = select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.id == version_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def get_version_by_id(
        self, version_id: UUID, *, for_update: bool = False
    ) -> StrategyVersion | None:
        statement = select(StrategyVersion).where(StrategyVersion.id == version_id)
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def latest_failed_version(
        self, strategy_id: UUID, source_code_hash: str
    ) -> StrategyVersion | None:
        return await self.session.scalar(
            select(StrategyVersion)
            .where(
                StrategyVersion.strategy_id == strategy_id,
                StrategyVersion.source_code_hash == source_code_hash,
                StrategyVersion.status == "PUBLISH_FAILED",
            )
            .order_by(StrategyVersion.version_no.desc())
            .limit(1)
            .with_for_update()
        )

    async def list_versions(
        self, strategy_id: UUID, *, page: int, page_size: int
    ) -> tuple[list[StrategyVersion], int]:
        condition = StrategyVersion.strategy_id == strategy_id
        total = int(
            await self.session.scalar(
                select(func.count()).select_from(StrategyVersion).where(condition)
            )
            or 0
        )
        rows = await self.session.scalars(
            select(StrategyVersion)
            .where(condition)
            .order_by(StrategyVersion.version_no.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return list(rows.all()), total
