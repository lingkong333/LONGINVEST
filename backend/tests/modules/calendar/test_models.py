from datetime import date, time
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql

from long_invest.modules.calendar.contracts import CalendarDayStatus
from long_invest.modules.calendar.models import (
    TradingCalendarCurrent,
    TradingCalendarDay,
    TradingCalendarVersion,
    TradingSession,
)


def ddl(table) -> str:
    return str(
        table.to_metadata(table.metadata).compile(dialect=postgresql.dialect())
    )


def test_calendar_models_define_unique_keys_and_restrict_parent_deletion() -> None:
    version_constraints = {
        tuple(item.columns.keys())
        for item in TradingCalendarVersion.__table__.constraints
    }
    day_constraints = {
        tuple(item.columns.keys()) for item in TradingCalendarDay.__table__.constraints
    }
    session_constraints = {
        tuple(item.columns.keys()) for item in TradingSession.__table__.constraints
    }

    assert ("market", "source", "source_version") in version_constraints
    assert ("market", "version_number") in version_constraints
    assert ("version_id", "trade_date") in day_constraints
    assert ("calendar_day_id", "sequence") in session_constraints
    current_fk = next(iter(TradingCalendarCurrent.__table__.c.version_id.foreign_keys))
    day_fk = next(iter(TradingCalendarDay.__table__.c.version_id.foreign_keys))
    session_fk = next(
        iter(TradingSession.__table__.c.calendar_day_id.foreign_keys)
    )
    assert current_fk.ondelete == "RESTRICT"
    assert day_fk.ondelete == "RESTRICT"
    assert session_fk.ondelete == "RESTRICT"


def test_version_days_and_sessions_are_guarded_as_immutable() -> None:
    for model in (TradingCalendarVersion, TradingCalendarDay, TradingSession):
        assert event.contains(model, "before_update", model._reject_mutation)
        assert event.contains(model, "before_delete", model._reject_mutation)


def test_calendar_model_defaults_preserve_parent_child_facts() -> None:
    version_id = uuid4()
    calendar_day = TradingCalendarDay(
        version_id=version_id,
        trade_date=date(2026, 7, 15),
        is_trading_day=True,
        status=CalendarDayStatus.CONFIRMED,
        source="git",
    )
    session = TradingSession(
        calendar_day_id=calendar_day.id,
        sequence=1,
        starts_at=time(9, 30),
        ends_at=time(11, 30),
    )

    assert calendar_day.id is not None
    assert session.id is not None
    assert calendar_day.status == CalendarDayStatus.CONFIRMED
    with pytest.raises(TypeError, match="immutable"):
        calendar_day._reject_mutation(None, None, calendar_day)
