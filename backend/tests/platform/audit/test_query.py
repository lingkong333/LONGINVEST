from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.platform.audit.application import AuditQueryApplication
from long_invest.platform.audit.query import AuditEventFilters, AuditEventQuery
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 22, 10, tzinfo=UTC)


class ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class Session:
    def __init__(self, *, rows=(), total=0):
        self.rows = rows
        self.total = total
        self.count_statement = None
        self.list_statement = None

    async def scalar(self, statement):
        self.count_statement = statement
        return self.total

    async def scalars(self, statement):
        self.list_statement = statement
        return ScalarRows(self.rows)


class Database:
    def __init__(self):
        self.opened = 0

    @asynccontextmanager
    async def session(self):
        self.opened += 1
        yield object()


class Query:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def list_events(self, filters, *, page, page_size):
        self.calls.append((filters, page, page_size))
        return self.result


def _event():
    return SimpleNamespace(
        id=uuid4(),
        occurred_at=NOW,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        action_code="TARGET_UPDATE",
        object_type="target",
        object_id="target-1",
        result="SUCCESS",
        before_summary={"version": 1},
        after_summary={"version": 2},
        reason="manual update",
        request_id="req-1",
        idempotency_key="idem-1",
        risk_level="HIGH",
    )


def _filters(**overrides):
    values = {
        "start_at": datetime(2026, 7, 1, tzinfo=UTC),
        "end_at": NOW,
    }
    values.update(overrides)
    return AuditEventFilters(**values)


@pytest.mark.anyio
async def test_query_returns_empty_paginated_result() -> None:
    session = Session()

    result = await AuditEventQuery(session).list_events(
        _filters(), page=1, page_size=50
    )

    assert result.items == ()
    assert result.total == 0
    assert (result.page, result.page_size) == (1, 50)


@pytest.mark.anyio
async def test_query_applies_every_filter_and_stable_recent_first_pagination() -> None:
    session = Session(rows=(_event(),), total=41)
    filters = _filters(
        actor_user_id="user-1",
        action_code="TARGET_UPDATE",
        object_type="target",
        object_id="target-1",
        result="SUCCESS",
        risk_level="HIGH",
        request_id="req-1",
    )

    result = await AuditEventQuery(session).list_events(filters, page=3, page_size=20)

    statement = session.list_statement
    sql = str(statement)
    params = statement.compile().params
    assert result.total == 41
    assert result.items[0].before_summary == {"version": 1}
    assert result.items[0].after_summary == {"version": 2}
    assert statement._offset_clause.value == 40
    assert statement._limit_clause.value == 20
    assert "audit_event.occurred_at DESC, audit_event.id DESC" in sql
    for expected in (
        "user-1",
        "TARGET_UPDATE",
        "target",
        "target-1",
        "SUCCESS",
        "HIGH",
        "req-1",
    ):
        assert expected in params.values()


@pytest.mark.anyio
async def test_application_defaults_to_the_most_recent_thirty_days() -> None:
    database = Database()
    expected = SimpleNamespace(items=(), total=0, page=1, page_size=50)
    query = Query(expected)
    application = AuditQueryApplication(
        database,
        query_factory=lambda _session: query,
        clock=lambda: NOW,
    )

    result = await application.list_events(page=1, page_size=50)

    assert result is expected
    filters, page, page_size = query.calls[0]
    assert filters.start_at == NOW - timedelta(days=30)
    assert filters.end_at == NOW
    assert (page, page_size) == (1, 50)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("start_at", "end_at", "code"),
    (
        (NOW, NOW - timedelta(seconds=1), "AUDIT_TIME_RANGE_INVALID"),
        (
            NOW - timedelta(days=90, seconds=1),
            NOW,
            "AUDIT_TIME_RANGE_TOO_WIDE",
        ),
        (
            datetime(2026, 7, 1),
            datetime(2026, 7, 2),
            "AUDIT_TIME_RANGE_INVALID",
        ),
    ),
)
async def test_application_rejects_invalid_time_windows_before_database_access(
    start_at, end_at, code
) -> None:
    database = Database()
    application = AuditQueryApplication(database)

    with pytest.raises(AppError) as error:
        await application.list_events(
            page=1,
            page_size=50,
            start_at=start_at,
            end_at=end_at,
        )

    assert error.value.code == code
    assert database.opened == 0
