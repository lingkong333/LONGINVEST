from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.modules.qfq.repository import QfqRepository


@pytest.mark.anyio
async def test_current_dataset_query_locks_and_refreshes_identity_map() -> None:
    session = AsyncMock()
    security_id = uuid4()

    await QfqRepository(session).current_dataset(security_id, for_update=True)

    statement = session.scalar.await_args_list[0].args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "FOR UPDATE" in str(compiled).upper()
    assert statement.get_execution_options()["populate_existing"] is True
    assert security_id in compiled.params.values()
    assert "CURRENT" in compiled.params.values()


@pytest.mark.anyio
async def test_run_transition_has_expected_prior_status_fence() -> None:
    session = AsyncMock()
    run_id = uuid4()
    session.scalar.return_value = run_id

    await QfqRepository(session).transition_run(
        run_id,
        expected_status="VALIDATING",
        status="COMMITTING",
    )

    statement = session.scalar.await_args_list[0].args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "qfq_refresh_run.status" in sql
    assert "VALIDATING" in compiled.params.values()
    assert "COMMITTING" in compiled.params.values()


@pytest.mark.anyio
async def test_zero_row_run_transition_raises_stable_conflict() -> None:
    session = AsyncMock()
    session.scalar.return_value = None

    with pytest.raises(Exception) as captured:
        await QfqRepository(session).transition_run(
            uuid4(),
            expected_status="VALIDATING",
            status="COMMITTING",
        )

    assert captured.value.code == "QFQ_REFRESH_CONFLICT"


@pytest.mark.anyio
async def test_security_advisory_lock_is_transaction_scoped() -> None:
    session = AsyncMock()

    await QfqRepository(session).lock_security(uuid4())

    statement = session.scalar.await_args.args[0]
    assert "pg_advisory_xact_lock" in str(statement)
