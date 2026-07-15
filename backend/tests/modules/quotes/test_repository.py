from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from long_invest.modules.quotes.models import QuoteCycle
from long_invest.modules.quotes.repository import QuoteCycleRepository


@pytest.mark.anyio
async def test_claim_cycle_uses_savepoint_and_rereads_conflict() -> None:
    session = Mock()
    session.flush = AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("duplicate"))
    )
    session.scalar = AsyncMock(return_value=Mock(spec=QuoteCycle))
    nested = AsyncMock()
    session.begin_nested.return_value = nested
    candidate = Mock(spec=QuoteCycle)
    candidate.idempotency_scope = "manual"
    candidate.idempotency_key = "same"
    claimed, created = await QuoteCycleRepository(session).claim_cycle(candidate)
    assert created is False and claimed is session.scalar.return_value
    session.rollback.assert_not_called()
    nested.__aenter__.assert_awaited_once()


@pytest.mark.anyio
async def test_finalize_query_uses_lock_and_refreshes_identity_map() -> None:
    session = AsyncMock()
    await QuoteCycleRepository(session).get_for_finalize(uuid4())
    statement = session.scalar.await_args.args[0]
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect())).upper()
    assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.anyio
async def test_lifecycle_query_uses_shared_lock_and_refresh() -> None:
    session = AsyncMock()
    await QuoteCycleRepository(session).get_for_update(uuid4())
    statement = session.scalar.await_args.args[0]
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect())).upper()
    assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.anyio
async def test_item_submission_query_serializes_and_refreshes() -> None:
    session = AsyncMock()
    cycle_id = uuid4()
    await QuoteCycleRepository(session).get_item_for_update(cycle_id, "600000.SH")
    statement = session.scalar.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "FOR UPDATE" in str(compiled).upper()
    assert statement.get_execution_options()["populate_existing"] is True
    assert cycle_id in compiled.params.values()
    assert "600000.SH" in compiled.params.values()
