from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.platform.outbox.service import TransactionalOutboxWriter


@pytest.mark.anyio
async def test_transactional_writer_uses_the_callers_session_and_dedupes() -> None:
    session = Mock()
    session.execute = AsyncMock()

    await TransactionalOutboxWriter().append(
        session=session,
        topic="security_master.updated",
        aggregate_type="security_master",
        aggregate_id="7",
        queue="domain-events",
        payload={"master_version": 7},
        dedupe_key="security-master:v7",
    )

    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in sql
    assert statement.compile().params["topic"] == "security_master.updated"
