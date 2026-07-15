from datetime import date, time

from long_invest.modules.calendar.contracts import (
    CalendarDayInput,
    CalendarDayStatus,
    CalendarImport,
    TradingSessionInput,
    validate_calendar_import,
)


def session(start: str, end: str) -> TradingSessionInput:
    return TradingSessionInput(
        starts_at=time.fromisoformat(start),
        ends_at=time.fromisoformat(end),
    )


def day(
    value: str,
    *,
    trading: bool = True,
    status: CalendarDayStatus = CalendarDayStatus.CONFIRMED,
    sessions: tuple[TradingSessionInput, ...] | None = None,
) -> CalendarDayInput:
    return CalendarDayInput(
        trade_date=date.fromisoformat(value),
        is_trading_day=trading,
        status=status,
        sessions=sessions if sessions is not None else (session("09:30", "11:30"),),
    )


def test_calendar_statuses_are_the_four_specified_values() -> None:
    assert {status.value for status in CalendarDayStatus} == {
        "CONFIRMED",
        "PROVISIONAL",
        "OVERRIDDEN",
        "MISSING",
    }


def test_valid_import_accepts_default_and_special_sessions() -> None:
    command = CalendarImport(
        market="CN_A",
        source="git",
        source_version="2026.1",
        idempotency_key="calendar-2026",
        days=(
            day(
                "2026-07-15",
                sessions=(
                    session("09:30", "11:30"),
                    session("13:00", "15:00"),
                ),
            ),
            day("2026-07-16", sessions=(session("10:00", "12:00"),)),
            day("2026-07-18", trading=False, sessions=()),
        ),
    )

    assert validate_calendar_import(command) == ()


def test_empty_import_is_rejected_as_incomplete() -> None:
    command = CalendarImport(
        market="CN_A",
        source="git",
        source_version="empty",
        idempotency_key="empty",
        days=(),
    )

    assert [issue.code for issue in validate_calendar_import(command)] == [
        "CALENDAR_IMPORT_EMPTY"
    ]


def test_import_validation_returns_every_item_issue_at_once() -> None:
    bad = CalendarImport(
        market="CN_A",
        source="git",
        source_version="bad",
        idempotency_key="calendar-bad",
        days=(
            day("2026-07-15", sessions=()),
            day("2026-07-15", trading=False, sessions=(session("09:30", "11:30"),)),
            day(
                "2026-07-16",
                sessions=(session("09:30", "11:30"), session("11:00", "12:00")),
            ),
            day("2026-07-17", sessions=(session("15:00", "09:30"),)),
        ),
    )

    issues = validate_calendar_import(bad)

    assert {issue.code for issue in issues} == {
        "CALENDAR_DATE_DUPLICATE",
        "CALENDAR_TRADING_DAY_SESSIONS_REQUIRED",
        "CALENDAR_NON_TRADING_DAY_HAS_SESSIONS",
        "CALENDAR_SESSION_OVERLAP",
        "CALENDAR_SESSION_OUT_OF_BOUNDS",
    }
    assert all(issue.path.startswith("days[") for issue in issues)


def test_only_confirmed_or_overridden_trading_days_are_automatic() -> None:
    assert day("2026-07-15").allows_automatic_trading
    assert day(
        "2026-07-15", status=CalendarDayStatus.OVERRIDDEN
    ).allows_automatic_trading
    assert not day(
        "2026-07-15", status=CalendarDayStatus.PROVISIONAL
    ).allows_automatic_trading
    assert not day(
        "2026-07-15", status=CalendarDayStatus.MISSING
    ).allows_automatic_trading
    assert not day("2026-07-15", trading=False, sessions=()).allows_automatic_trading
