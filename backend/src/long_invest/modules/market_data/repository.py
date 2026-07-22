from datetime import date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.market_data.contracts import QualityIssueStatus
from long_invest.modules.market_data.models import (
    CorporateActionFact,
    CorporateActionFetchBatch,
    DataQualityIssue,
)


class CorporateActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def get_batch(self, batch_id: UUID) -> CorporateActionFetchBatch | None:
        return await self._session.get(CorporateActionFetchBatch, batch_id)

    async def list_event_facts_for_update(
        self,
        *,
        security_id: UUID,
        source: str,
        source_event_ids: tuple[str, ...],
    ) -> list[CorporateActionFact]:
        if not source_event_ids:
            return []
        rows = await self._session.scalars(
            select(CorporateActionFact)
            .where(
                CorporateActionFact.security_id == security_id,
                CorporateActionFact.source == source,
                CorporateActionFact.source_event_id.in_(source_event_ids),
            )
            .order_by(
                CorporateActionFact.source_event_id,
                CorporateActionFact.revision_no,
            )
            .with_for_update()
        )
        return list(rows.all())

    async def claim_fetch(
        self,
        batch: CorporateActionFetchBatch,
        facts: tuple[CorporateActionFact, ...],
    ) -> tuple[CorporateActionFetchBatch | None, bool]:
        try:
            async with self._session.begin_nested():
                self._session.add(batch)
                self._session.add_all(facts)
                await self._session.flush()
        except IntegrityError:
            existing = await self.get_batch(batch.id)
            return existing, False
        return batch, True

    async def list_covering_batches(
        self,
        *,
        security_id: UUID,
        start_date: date,
        end_date: date,
        as_of: datetime,
    ) -> list[CorporateActionFetchBatch]:
        rows = await self._session.scalars(
            select(CorporateActionFetchBatch)
            .where(
                CorporateActionFetchBatch.security_id == security_id,
                CorporateActionFetchBatch.status == "SUCCESS",
                CorporateActionFetchBatch.coverage_start <= start_date,
                CorporateActionFetchBatch.coverage_end >= end_date,
                CorporateActionFetchBatch.observed_at <= as_of,
                CorporateActionFetchBatch.fetched_at <= as_of,
            )
            .order_by(
                CorporateActionFetchBatch.observed_at.desc(),
                CorporateActionFetchBatch.fetched_at.desc(),
                CorporateActionFetchBatch.id,
            )
        )
        return list(rows.all())

    async def list_facts(
        self,
        *,
        security_id: UUID,
        source: str,
        start_date: date,
        end_date: date,
        as_of: datetime,
        observed_through: datetime,
    ) -> list[CorporateActionFact]:
        rows = await self._session.scalars(
            select(CorporateActionFact)
            .join(
                CorporateActionFetchBatch,
                CorporateActionFetchBatch.id == CorporateActionFact.batch_id,
            )
            .where(
                CorporateActionFact.security_id == security_id,
                CorporateActionFact.source == source,
                CorporateActionFact.effective_date >= start_date,
                CorporateActionFact.effective_date <= end_date,
                CorporateActionFact.observed_at <= observed_through,
                CorporateActionFetchBatch.status == "SUCCESS",
                CorporateActionFetchBatch.fetched_at <= as_of,
            )
            .order_by(
                CorporateActionFact.source_event_id,
                CorporateActionFact.revision_no.desc(),
                CorporateActionFact.id,
            )
        )
        return list(rows.all())


class QualityIssueRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def find_by_dedupe_key(
        self,
        dedupe_key: str,
    ) -> DataQualityIssue | None:
        return await self._session.scalar(
            select(DataQualityIssue).where(DataQualityIssue.dedupe_key == dedupe_key)
        )

    async def get_for_update(self, issue_id: UUID) -> DataQualityIssue | None:
        return await self._session.scalar(
            select(DataQualityIssue)
            .where(DataQualityIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def add(self, record: DataQualityIssue) -> None:
        self._session.add(record)

    async def flush(self) -> None:
        await self._session.flush()

    async def claim_issue(
        self,
        record: DataQualityIssue,
    ) -> tuple[DataQualityIssue, bool]:
        try:
            async with self._session.begin_nested():
                self.add(record)
                await self.flush()
        except IntegrityError as exc:
            existing = await self.find_by_dedupe_key(record.dedupe_key)
            if existing is None:
                raise exc
            return existing, False
        return record, True

    async def list(
        self,
        *,
        status: QualityIssueStatus | None = None,
        issue_type: str | None = None,
        symbol: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> list[DataQualityIssue]:
        if page < 1 or page_size < 1:
            raise ValueError("page and page_size must be positive")
        statement = self._filtered_query(
            status=status,
            issue_type=issue_type,
            symbol=symbol,
        )
        rows = await self._session.scalars(
            statement.order_by(
                DataQualityIssue.last_seen_at.desc(),
                DataQualityIssue.id,
            )
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        return list(rows.all())

    async def count(
        self,
        *,
        status: QualityIssueStatus | None = None,
        issue_type: str | None = None,
        symbol: str | None = None,
    ) -> int:
        statement = select(func.count()).select_from(DataQualityIssue)
        statement = self._apply_filters(
            statement,
            status=status,
            issue_type=issue_type,
            symbol=symbol,
        )
        return int(await self._session.scalar(statement) or 0)

    @classmethod
    def _filtered_query(
        cls,
        *,
        status: QualityIssueStatus | None,
        issue_type: str | None,
        symbol: str | None,
    ):
        return cls._apply_filters(
            select(DataQualityIssue),
            status=status,
            issue_type=issue_type,
            symbol=symbol,
        )

    @staticmethod
    def _apply_filters(statement, *, status, issue_type, symbol):
        if status is not None:
            statement = statement.where(DataQualityIssue.status == status)
        if issue_type is not None:
            statement = statement.where(DataQualityIssue.issue_type == issue_type)
        if symbol is not None:
            statement = statement.where(DataQualityIssue.symbol == symbol)
        return statement
