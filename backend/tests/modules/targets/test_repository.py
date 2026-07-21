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


@pytest.mark.anyio
async def test_list_bindings_has_stable_order() -> None:
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars = AsyncMock(return_value=result)
    repository = TargetRepository(session)

    assert await repository.list_bindings() == ()

    statement = session.scalars.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ORDER BY subscription_target_binding.created_at" in sql
    assert "subscription_target_binding.id" in sql
