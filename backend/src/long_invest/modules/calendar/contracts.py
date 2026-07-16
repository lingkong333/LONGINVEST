from __future__ import annotations

from datetime import date, time, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalendarDayStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PROVISIONAL = "PROVISIONAL"
    OVERRIDDEN = "OVERRIDDEN"
    MISSING = "MISSING"


class CalendarAuditContext(StrictContract):
    request_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    trusted_ip: str = Field(min_length=1, max_length=64)


class TradingSessionInput(StrictContract):
    starts_at: time
    ends_at: time


def default_trading_sessions() -> tuple[TradingSessionInput, ...]:
    return (
        TradingSessionInput(starts_at=time(9, 30), ends_at=time(11, 30)),
        TradingSessionInput(starts_at=time(13), ends_at=time(15)),
    )


class CalendarDayInput(StrictContract):
    trade_date: date
    is_trading_day: bool
    status: CalendarDayStatus
    sessions: tuple[TradingSessionInput, ...] = ()
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def apply_default_sessions(cls, value: Any) -> Any:
        if isinstance(value, dict) and "sessions" not in value:
            return {
                **value,
                "sessions": default_trading_sessions()
                if value.get("is_trading_day")
                else (),
            }
        return value

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
    audit_context: CalendarAuditContext | None = Field(default=None, exclude=True)


class CalendarValidationIssue(StrictContract):
    code: str
    path: str
    message: str


class CalendarVersionResult(StrictContract):
    version_id: UUID | None = None
    version_number: int | None = None
    created: bool = False
    issues: tuple[CalendarValidationIssue, ...] = ()
    warnings: tuple[CalendarValidationIssue, ...] = ()


class OverrideCalendarDay(StrictContract):
    market: str = Field(min_length=1, max_length=16)
    trade_date: date
    is_trading_day: bool
    sessions: tuple[TradingSessionInput, ...] = ()
    expected_current_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=500)
    audit_context: CalendarAuditContext | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def apply_default_sessions(cls, value: Any) -> Any:
        if isinstance(value, dict) and "sessions" not in value:
            return {
                **value,
                "sessions": default_trading_sessions()
                if value.get("is_trading_day")
                else (),
            }
        return value


class RestoreCalendarVersion(StrictContract):
    market: str = Field(min_length=1, max_length=16)
    version_id: UUID
    expected_current_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)
    audit_context: CalendarAuditContext | None = Field(default=None, exclude=True)


class CalendarCoverage(StrictContract):
    market: str
    from_date: date
    confirmed_through: date | None
    future_confirmed_days: int = Field(ge=0)
    level: str
    current_version_id: UUID | None
    missing_today: bool = False


class TradingDateWindow(StrictContract):
    market: str = Field(min_length=1, max_length=16)
    start: date
    end: date
    version_id: UUID
    version_number: int = Field(ge=1)
    dates: tuple[date, ...]

    @model_validator(mode="after")
    def validate_window(self) -> TradingDateWindow:
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if any(item < self.start or item > self.end for item in self.dates):
            raise ValueError("trading dates must stay inside the window")
        if any(
            current <= previous
            for previous, current in zip(self.dates, self.dates[1:], strict=False)
        ):
            raise ValueError("trading dates must be strictly ascending")
        return self


class CalendarEvent(StrictContract):
    event_type: str
    aggregate_id: str
    idempotency_key: str
    payload: dict[str, Any]


class CalendarEventSink(Protocol):
    async def append(self, event: CalendarEvent) -> object: ...


def validate_calendar_import(
    command: CalendarImport,
) -> tuple[CalendarValidationIssue, ...]:
    issues: list[CalendarValidationIssue] = []
    if not command.days:
        issues.append(
            CalendarValidationIssue(
                code="CALENDAR_IMPORT_EMPTY",
                path="days",
                message="日历导入必须包含至少一个日期",
            )
        )
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


def validate_calendar_coverage(
    days: tuple[CalendarDayInput, ...],
    *,
    from_date: date,
    required_days: int,
) -> tuple[CalendarValidationIssue, ...]:
    by_date = {item.trade_date: item for item in days}
    issues: list[CalendarValidationIssue] = []
    for offset in range(required_days):
        wanted = from_date + timedelta(days=offset)
        item = by_date.get(wanted)
        if item is None or item.status not in {
            CalendarDayStatus.CONFIRMED,
            CalendarDayStatus.OVERRIDDEN,
        }:
            issues.append(
                CalendarValidationIssue(
                    code="CALENDAR_COVERAGE_GAP",
                    path=f"days[{wanted.isoformat()}]",
                    message="日期缺失或尚未确认",
                )
            )
    return tuple(issues)
