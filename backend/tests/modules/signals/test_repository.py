from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.modules.signals.models import SignalEvaluation, SignalEvent
from long_invest.modules.signals.repository import SignalRepository


@pytest.mark.anyio
async def test_lock_state_uses_for_update_and_initializes_unknown_once():
    session = MagicMock()
    expected = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=expected)
    session.flush = AsyncMock()
    repository = SignalRepository(session)
    subscription_id = uuid4()

    state = await repository.lock_or_create_state(subscription_id)

    statement = session.scalar.await_args.args[0]
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
    insert_sql = str(
        session.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "ON CONFLICT (subscription_id) DO NOTHING" in insert_sql
    assert "'UNKNOWN'" in insert_sql
    assert state is expected


@pytest.mark.anyio
async def test_writes_flush_but_never_commit():
    session = MagicMock()
    session.flush = AsyncMock()
    repository = SignalRepository(session)
    evaluation = MagicMock(spec=SignalEvaluation)
    event = MagicMock(spec=SignalEvent)
    await repository.add_evaluation(evaluation)
    await repository.add_event(event)
    await repository.flush()
    assert session.add.call_args_list[0].args == (evaluation,)
    assert session.add.call_args_list[1].args == (event,)
    session.flush.assert_awaited_once()
    assert not hasattr(session, "commit") or not session.commit.called


@pytest.mark.anyio
async def test_history_queries_have_stable_descending_order():
    session = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = []
    session.scalars = AsyncMock(return_value=scalars)
    repository = SignalRepository(session)

    assert await repository.list_evaluations(page=2, page_size=3) == ()
    evaluation_sql = str(
        session.scalars.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "ORDER BY signal_evaluation.created_at DESC" in evaluation_sql
    assert "signal_evaluation.id DESC" in evaluation_sql
    assert "LIMIT 3 OFFSET 3" in evaluation_sql

    assert await repository.list_events(page=1, page_size=2) == ()
    event_sql = str(
        session.scalars.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "ORDER BY signal_event.created_at DESC" in event_sql
    assert "signal_event.id DESC" in event_sql
