from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.modules.targets.repository import TargetRepository


@pytest.mark.anyio
async def test_lock_binding_uses_row_lock() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    repository = TargetRepository(session)

    await repository.lock_binding(uuid4())

    statement = session.scalar.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


@pytest.mark.anyio
async def test_repository_writes_without_committing() -> None:
    session = MagicMock()
    session.flush = AsyncMock()
    repository = TargetRepository(session)
    revision = MagicMock()

    await repository.persist_revision(revision)
    await repository.flush()

    session.add.assert_called_once_with(revision)
    session.flush.assert_awaited_once()
    assert not hasattr(session, "commit") or not session.commit.called
