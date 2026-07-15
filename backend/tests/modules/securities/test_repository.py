from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from long_invest.modules.securities.contracts import Market, UniverseQuery
from long_invest.modules.securities.models import Security
from long_invest.modules.securities.repository import SecurityRepository


@pytest.mark.anyio
async def test_list_is_server_paginated_and_stably_ordered() -> None:
    session = AsyncMock()
    scalars = Mock()
    scalars.all.return_value = []
    session.scalars.return_value = scalars

    items = await SecurityRepository(session).list(page=3, page_size=20)

    statement = session.scalars.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert compiled.params["param_1"] == 20
    assert compiled.params["param_2"] == 40
    assert "ORDER BY security.symbol" in str(compiled)
    assert items == []


@pytest.mark.anyio
async def test_search_matches_symbol_or_name_and_remains_paginated() -> None:
    session = AsyncMock()
    scalars = Mock()
    scalars.all.return_value = []
    session.scalars.return_value = scalars

    await SecurityRepository(session).search("浦发", page=1, page_size=10)

    statement = session.scalars.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "security.symbol ILIKE" in sql
    assert "security.name ILIKE" in sql
    assert "LIMIT" in sql


@pytest.mark.anyio
async def test_get_many_deduplicates_input_and_returns_symbol_mapping() -> None:
    security = Mock(spec=Security)
    security.symbol = "600000.SH"
    session = AsyncMock()
    scalars = Mock()
    scalars.all.return_value = [security]
    session.scalars.return_value = scalars

    result = await SecurityRepository(session).get_many(
        ["600000.SH", "600000.SH"]
    )

    assert result == {"600000.SH": security}


@pytest.mark.anyio
async def test_universe_query_filters_a_shares_without_excluding_suspensions() -> None:
    session = AsyncMock()
    scalars = Mock()
    scalars.all.return_value = []
    session.scalars.return_value = scalars

    await SecurityRepository(session).list_for_universe(
        UniverseQuery(markets=(Market.SH,))
    )

    statement = session.scalars.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "security.market IN" in sql
    assert "security.security_type IN" in sql
    assert "security.listing_status IN" in sql
    where_clause = sql.split("WHERE", maxsplit=1)[1]
    assert "security.is_suspended" not in where_clause


@pytest.mark.anyio
async def test_read_snapshot_loads_its_frozen_items() -> None:
    session = AsyncMock()
    snapshot_id = uuid4()

    await SecurityRepository(session).get_universe_snapshot(snapshot_id)

    statement = session.scalar.await_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True
