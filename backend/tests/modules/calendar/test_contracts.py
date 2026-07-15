from datetime import date, time

from long_invest.modules.calendar.contracts import (
    CalendarDayInput,
    CalendarDayStatus,
    CalendarImport,
    TradingSessionInput,
    validate_calendar_coverage,
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


def test_omitted_trading_sessions_use_the_two_default_a_share_periods() -> None:
    trading = CalendarDayInput(
        trade_date=date(2026, 7, 15),
        is_trading_day=True,
        status=CalendarDayStatus.CONFIRMED,
    )
    closed = CalendarDayInput(
        trade_date=date(2026, 7, 18),
        is_trading_day=False,
        status=CalendarDayStatus.CONFIRMED,
    )

    assert [(item.starts_at, item.ends_at) for item in trading.sessions] == [
        (time(9, 30), time(11, 30)),
        (time(13), time(15)),
    ]
    assert closed.sessions == ()


def test_continuous_coverage_reports_every_missing_or_unconfirmed_natural_day() -> None:
    start = date(2026, 7, 15)
    days = (
        day("2026-07-15"),
        day("2026-07-17", status=CalendarDayStatus.PROVISIONAL),
        day("2026-07-18", status=CalendarDayStatus.MISSING),
    )

    issues = validate_calendar_coverage(days, from_date=start, required_days=4)

    assert [(item.code, item.path) for item in issues] == [
        ("CALENDAR_COVERAGE_GAP", "days[2026-07-16]"),
        ("CALENDAR_COVERAGE_GAP", "days[2026-07-17]"),
        ("CALENDAR_COVERAGE_GAP", "days[2026-07-18]"),
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
