from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.calendar.contracts import TradingDateWindow
from long_invest.modules.calendar.repository import CalendarRepository
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


class CalendarApplication:
    def __init__(
        self,
        database: Database,
        *,
        repository_factory: Callable[..., Any] = CalendarRepository,
    ) -> None:
        self._database = database
        self._repository_factory = repository_factory

    async def trading_dates(
        self,
        start: date,
        end: date,
        market: str = "CN_A",
    ) -> TradingDateWindow:
        if start > end:
            raise AppError(
                code="CALENDAR_DATE_RANGE_INVALID",
                message="开始日期不能晚于结束日期",
                status_code=422,
            )
        try:
            async with self._database.session() as session:
                record = await self._repository_factory(session).trading_date_window(
                    market, start, end
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc
        if record is None:
            raise AppError(
                code="CALENDAR_CURRENT_NOT_FOUND",
                message="当前日历不存在",
                status_code=404,
            )
        return TradingDateWindow(
            market=market,
            start=start,
            end=end,
            version_id=record.version_id,
            version_number=record.version_number,
            dates=record.dates,
        )


def get_calendar_application() -> CalendarApplication:
    return CalendarApplication(get_database())


def _backend_unavailable() -> AppError:
    return AppError(
        code="CALENDAR_BACKEND_UNAVAILABLE",
        message="交易日历服务暂时不可用",
        status_code=503,
    )
