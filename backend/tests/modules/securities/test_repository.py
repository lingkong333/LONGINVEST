from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from long_invest.modules.securities.contracts import Market, UniverseQuery
from long_invest.modules.securities.models import Security
from long_invest.modules.securities.repository import SecurityRepository
from long_invest.platform.errors import AppError


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
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "security.market IN" in sql
    assert "security.security_type IN" in sql
    assert "security.listing_status IN" in sql
    status_values = next(
        value
        for key, value in compiled.params.items()
        if key.startswith("listing_status_")
    )
    assert status_values == ["LISTED", "SUSPENDED"]
    where_clause = sql.split("WHERE", maxsplit=1)[1]
    assert "security.is_suspended" not in where_clause


@pytest.mark.anyio
async def test_read_snapshot_loads_its_frozen_items() -> None:
    session = AsyncMock()
    snapshot_id = uuid4()

    await SecurityRepository(session).get_universe_snapshot(snapshot_id)

    statement = session.scalar.await_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.anyio
async def test_master_update_lock_uses_transaction_scoped_postgres_lock() -> None:
    session = AsyncMock()

    await SecurityRepository(session).lock_master_updates()

    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "pg_advisory_xact_lock" in sql


@pytest.mark.anyio
async def test_unresolved_master_version_collision_is_a_stable_conflict() -> None:
    session = Mock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock(
        side_effect=IntegrityError("insert", {}, RuntimeError("race"))
    )
    nested = AsyncMock()
    session.begin_nested.return_value = nested
    record = Mock(
        source="eastmoney",
        idempotency_key="key-1",
        source_version="version-1",
    )

    with pytest.raises(AppError) as captured:
        await SecurityRepository(session).claim_master_import(record)

    assert captured.value.code == "SECURITY_MASTER_VERSION_CONFLICT"
    assert captured.value.status_code == 409


@pytest.mark.anyio
async def test_same_version_collision_returns_the_formal_winner() -> None:
    session = Mock()
    winner = Mock()
    session.scalar = AsyncMock(return_value=winner)
    session.flush = AsyncMock(
        side_effect=IntegrityError("insert", {}, RuntimeError("race"))
    )
    session.begin_nested.return_value = AsyncMock()
    contender = Mock(
        source="eastmoney",
        idempotency_key="same-key",
        source_version="same-version",
    )

    claimed, created = await SecurityRepository(session).claim_master_import(
        contender
    )

    assert claimed is winner
    assert created is False


@pytest.mark.anyio
async def test_complete_snapshot_reads_all_existing_securities_for_update() -> None:
    session = AsyncMock()
    scalars = Mock()
    scalars.all.return_value = []
    session.scalars.return_value = scalars

    await SecurityRepository(session).list_all_for_update()

    statement = session.scalars.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "ORDER BY SECURITY.SYMBOL" in sql
    assert "FOR UPDATE" in sql
