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

    assert await repository.list_bindings(page=2, page_size=2) == ()

    statement = session.scalars.await_args.args[0]
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "ORDER BY subscription_target_binding.created_at DESC" in sql
    assert "subscription_target_binding.id DESC" in sql
    assert "LIMIT 2 OFFSET 2" in sql
    assert "current_revision_id IS NOT NULL" in sql
    assert "activated_at IS NOT NULL" in sql


@pytest.mark.anyio
async def test_list_bindings_rejects_invalid_pages_and_counts_total() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=7)
    repository = TargetRepository(session)

    assert await repository.count_bindings() == 7
    for page, page_size in ((0, 50), (1, 0), (1, 201)):
        with pytest.raises(ValueError):
            await repository.list_bindings(page=page, page_size=page_size)
