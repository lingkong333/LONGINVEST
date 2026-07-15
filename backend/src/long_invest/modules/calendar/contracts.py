from __future__ import annotations

from datetime import date, time, timedelta, timezone
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalendarDayStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PROVISIONAL = "PROVISIONAL"
    OVERRIDDEN = "OVERRIDDEN"
    MISSING = "MISSING"


class TradingSessionInput(StrictContract):
    starts_at: time
    ends_at: time


class CalendarDayInput(StrictContract):
    trade_date: date
    is_trading_day: bool
    status: CalendarDayStatus
    sessions: tuple[TradingSessionInput, ...] = ()
    note: str | None = None

    @property
    def allows_automatic_trading(self) -> bool:
        return self.is_trading_day and self.status in {
            CalendarDayStatus.CONFIRMED,
            CalendarDayStatus.OVERRIDDEN,
        }


class CalendarImport(StrictContract):
    market: str = Field(min_length=1, max_length=16)
    source: str = Field(min_length=1, max_length=64)
    source_version: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_current_version: int | None = Field(default=None, ge=1)
    days: tuple[CalendarDayInput, ...]
    reason: str | None = Field(default=None, max_length=500)


class CalendarValidationIssue(StrictContract):
    code: str
    path: str
    message: str


class CalendarVersionResult(StrictContract):
    version_id: UUID | None = None
    version_number: int | None = None
    created: bool = False
    issues: tuple[CalendarValidationIssue, ...] = ()


class OverrideCalendarDay(StrictContract):
    market: str = Field(min_length=1, max_length=16)
    trade_date: date
    is_trading_day: bool
    sessions: tuple[TradingSessionInput, ...] = ()
    expected_current_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=500)


class RestoreCalendarVersion(StrictContract):
    market: str = Field(min_length=1, max_length=16)
    version_id: UUID
    expected_current_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)


class CalendarCoverage(StrictContract):
    market: str
    from_date: date
    confirmed_through: date | None
    future_confirmed_days: int = Field(ge=0)
    level: str
    current_version_id: UUID | None
    missing_today: bool = False


def validate_calendar_import(
    command: CalendarImport,
) -> tuple[CalendarValidationIssue, ...]:
    issues: list[CalendarValidationIssue] = []
    seen: dict[date, int] = {}
    for day_index, calendar_day in enumerate(command.days):
        path = f"days[{day_index}]"
        if calendar_day.trade_date in seen:
            issues.append(
                CalendarValidationIssue(
                    code="CALENDAR_DATE_DUPLICATE",
                    path=f"{path}.trade_date",
                    message="日期在同一导入中重复",
                )
            )
        else:
            seen[calendar_day.trade_date] = day_index

        if calendar_day.is_trading_day and not calendar_day.sessions:
            issues.append(
                CalendarValidationIssue(
                    code="CALENDAR_TRADING_DAY_SESSIONS_REQUIRED",
                    path=f"{path}.sessions",
                    message="交易日必须包含至少一个交易时段",
                )
            )
        if not calendar_day.is_trading_day and calendar_day.sessions:
            issues.append(
                CalendarValidationIssue(
                    code="CALENDAR_NON_TRADING_DAY_HAS_SESSIONS",
                    path=f"{path}.sessions",
                    message="非交易日不能包含交易时段",
                )
            )

        valid_sessions: list[tuple[int, TradingSessionInput]] = []
        for session_index, session in enumerate(calendar_day.sessions):
            session_path = f"{path}.sessions[{session_index}]"
            if session.starts_at >= session.ends_at:
                issues.append(
                    CalendarValidationIssue(
                        code="CALENDAR_SESSION_OUT_OF_BOUNDS",
                        path=session_path,
                        message="交易时段必须在同一北京时间日期内且开始早于结束",
                    )
                )
            else:
                valid_sessions.append((session_index, session))

        ordered = sorted(valid_sessions, key=lambda item: item[1].starts_at)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current[1].starts_at < previous[1].ends_at:
                issues.append(
                    CalendarValidationIssue(
                        code="CALENDAR_SESSION_OVERLAP",
                        path=f"{path}.sessions[{current[0]}]",
                        message="交易时段不能重叠",
                    )
                )
    return tuple(issues)
