from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from long_invest.platform.database.batching import (
    execute_atomic_batches,
    execute_isolated_batches,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSession:
    def __init__(self, *, fail_on: int | None = None) -> None:
        self.fail_on = fail_on
        self.executed = []
        self.savepoints = 0

    async def execute(self, statement) -> None:
        self.executed.append(statement)
        if self.fail_on == len(self.executed):
            raise SQLAlchemyError("write failed")

    @asynccontextmanager
    async def begin_nested(self):
        self.savepoints += 1
        yield


def statement(values: tuple[int, ...]):
    return text(f"SELECT {values[0]}")


@pytest.mark.anyio
async def test_atomic_batches_propagate_failure_to_caller_transaction() -> None:
    session = FakeSession(fail_on=2)

    with pytest.raises(SQLAlchemyError):
        await execute_atomic_batches(
            session,
            [1, 2, 3],
            batch_size=2,
            statement_factory=statement,
        )

    assert len(session.executed) == 2
    assert session.savepoints == 0


@pytest.mark.anyio
async def test_isolated_batches_report_failures_and_keep_other_batches() -> None:
    session = FakeSession(fail_on=2)

    result = await execute_isolated_batches(
        session,
        [1, 2, 3, 4, 5],
        batch_size=2,
        statement_factory=statement,
    )

    assert result.attempted == 5
    assert result.succeeded == 3
    assert result.failures[0].first_item == 2
    assert result.failures[0].item_count == 2
    assert result.failures[0].error_type == "SQLAlchemyError"
    assert session.savepoints == 3
    assert len(session.executed) == 3


@pytest.mark.anyio
async def test_batch_writer_rejects_non_positive_batch_size() -> None:
    with pytest.raises(ValueError):
        await execute_atomic_batches(
            FakeSession(),
            [1],
            batch_size=0,
            statement_factory=statement,
        )
