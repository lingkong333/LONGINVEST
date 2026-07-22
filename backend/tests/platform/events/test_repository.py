from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.platform.events.repository import PostgresEventSource


class FakeResult:
    def all(self):
        return [
            SimpleNamespace(
                stream_sequence=8,
                topic="jobs.dispatch",
                aggregate_id="job-8",
            )
        ]


class FakeSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return FakeResult()


class FakeDatabase:
    def __init__(self) -> None:
        self.current_session = FakeSession()

    @asynccontextmanager
    async def session(self):
        yield self.current_session


@pytest.mark.anyio
async def test_fetch_uses_sequence_pagination_and_never_selects_payload() -> None:
    database = FakeDatabase()
    source = PostgresEventSource(database)  # type: ignore[arg-type]

    events = await source.fetch_after(7, limit=100)

    assert events[0].sequence == 8
    statement = str(
        database.current_session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "event_outbox.stream_sequence > 7" in statement
    assert "event_outbox.topic IN" in statement
    assert "ORDER BY event_outbox.stream_sequence" in statement
    selected_columns = statement.split("FROM event_outbox", maxsplit=1)[0]
    assert "payload" not in selected_columns
