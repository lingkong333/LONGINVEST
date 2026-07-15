from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.market_data.contracts import QualityIssueStatus
from long_invest.modules.market_data.models import DataQualityIssue


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
