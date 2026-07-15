from datetime import date
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from long_invest.modules.calendar.contracts import CalendarDayStatus
from long_invest.modules.calendar.models import (
    TradingCalendarCurrent,
    TradingCalendarDay,
    TradingCalendarVersion,
)


class CalendarRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_current(self, market: str) -> TradingCalendarCurrent | None:
        return await self._session.scalar(
            select(TradingCalendarCurrent).where(
                TradingCalendarCurrent.market == market
            )
        )

    async def lock_current(self, market: str) -> TradingCalendarCurrent | None:
        return await self._session.scalar(
            select(TradingCalendarCurrent)
            .where(TradingCalendarCurrent.market == market)
            .with_for_update()
        )

    async def get_day(
        self,
        market: str,
        trade_date: date,
    ) -> TradingCalendarDay | None:
        return await self._session.scalar(
            self._current_day_query(market).where(
                TradingCalendarDay.trade_date == trade_date
            )
        )

    async def list_days(
        self,
        market: str,
        from_date: date,
        through_date: date,
    ) -> list[TradingCalendarDay]:
        rows = await self._session.scalars(
            self._current_day_query(market)
            .where(TradingCalendarDay.trade_date.between(from_date, through_date))
            .order_by(TradingCalendarDay.trade_date)
        )
        return list(rows.all())

    async def next_trading_day(
        self, market: str, after_date: date
    ) -> TradingCalendarDay | None:
        return await self._session.scalar(
            self._automatic_day_query(market)
            .where(TradingCalendarDay.trade_date > after_date)
            .order_by(TradingCalendarDay.trade_date)
            .limit(1)
        )

    async def previous_trading_day(
        self, market: str, before_date: date
    ) -> TradingCalendarDay | None:
        return await self._session.scalar(
            self._automatic_day_query(market)
            .where(TradingCalendarDay.trade_date < before_date)
            .order_by(TradingCalendarDay.trade_date.desc())
            .limit(1)
        )

    async def get_version(
        self, version_id: UUID
    ) -> TradingCalendarVersion | None:
        return await self._session.scalar(
            select(TradingCalendarVersion)
            .where(TradingCalendarVersion.id == version_id)
            .options(
                selectinload(TradingCalendarVersion.days).selectinload(
                    TradingCalendarDay.sessions
                )
            )
        )

    async def list_versions(self, market: str) -> list[TradingCalendarVersion]:
        result = await self._session.scalars(
            select(TradingCalendarVersion)
            .where(TradingCalendarVersion.market == market)
            .order_by(TradingCalendarVersion.version_number.desc())
        )
        return list(result.all())

    async def find_by_idempotency(
        self, market: str, idempotency_key: str
    ) -> TradingCalendarVersion | None:
        return await self._session.scalar(
            select(TradingCalendarVersion).where(
                TradingCalendarVersion.market == market,
                TradingCalendarVersion.idempotency_key == idempotency_key,
            )
        )

    async def confirmed_through(
        self, market: str, from_date: date
    ) -> date | None:
        rows = await self._session.scalars(
            select(TradingCalendarDay)
            .join(
                TradingCalendarCurrent,
                TradingCalendarCurrent.version_id == TradingCalendarDay.version_id,
            )
            .where(
                TradingCalendarCurrent.market == market,
                TradingCalendarDay.trade_date >= from_date,
            )
            .order_by(TradingCalendarDay.trade_date)
        )
        return _continuous_confirmed_through(list(rows.all()), from_date)

    async def next_version_number(self, market: str) -> int:
        value = await self._session.scalar(
            select(func.max(TradingCalendarVersion.version_number)).where(
                TradingCalendarVersion.market == market
            )
        )
        return (value or 0) + 1

    async def add_version(self, version: TradingCalendarVersion) -> None:
        self._session.add(version)
        await self._session.flush()

    async def switch_current(
        self,
        *,
        market: str,
        version_id: UUID,
        expected_pointer_version: int | None,
    ) -> bool:
        if expected_pointer_version is None:
            self._session.add(
                TradingCalendarCurrent(
                    market=market,
                    version_id=version_id,
                    pointer_version=1,
                )
            )
            await self._session.flush()
            return True
        changed = await self._session.scalar(
            update(TradingCalendarCurrent)
            .where(
                TradingCalendarCurrent.market == market,
                TradingCalendarCurrent.pointer_version
                == expected_pointer_version,
            )
            .values(
                version_id=version_id,
                pointer_version=expected_pointer_version + 1,
                switched_at=func.now(),
            )
            .returning(TradingCalendarCurrent.version_id)
        )
        return changed is not None

    def _current_day_query(self, market: str):
        return (
            select(TradingCalendarDay)
            .join(
                TradingCalendarCurrent,
                TradingCalendarCurrent.version_id == TradingCalendarDay.version_id,
            )
            .where(TradingCalendarCurrent.market == market)
            .options(selectinload(TradingCalendarDay.sessions))
        )

    def _automatic_day_query(self, market: str):
        return self._current_day_query(market).where(
            TradingCalendarDay.is_trading_day.is_(True),
            TradingCalendarDay.status.in_(
                (CalendarDayStatus.CONFIRMED, CalendarDayStatus.OVERRIDDEN)
            ),
        )


def _continuous_confirmed_through(
    rows: list[TradingCalendarDay], from_date: date
) -> date | None:
    expected = from_date
    through = None
    for row in rows:
        if row.trade_date != expected or row.status not in {
            CalendarDayStatus.CONFIRMED,
            CalendarDayStatus.OVERRIDDEN,
        }:
            break
        through = row.trade_date
        expected = expected.fromordinal(expected.toordinal() + 1)
    return through
