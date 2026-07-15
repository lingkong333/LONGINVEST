from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from long_invest.modules.securities.contracts import Market, UniverseQuery
from long_invest.modules.securities.models import (
    Security,
    SecurityUniverseSnapshot,
    SecurityUniverseSnapshotItem,
)
from long_invest.modules.securities.repository import SecurityRepository
from long_invest.platform.errors import AppError


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_pg_uuid_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "CHAR(32)"


class AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instance) -> None:
        self._session.add(instance)

    def add_all(self, instances) -> None:
        self._session.add_all(instances)

    async def flush(self, instances=None) -> None:
        self._session.flush(instances)

    async def scalar(self, statement):
        return self._session.scalar(statement)


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
async def test_saved_snapshot_does_not_follow_later_security_changes() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE security (
                id CHAR(32) PRIMARY KEY,
                symbol VARCHAR(16) NOT NULL UNIQUE,
                exchange_code VARCHAR(32) NOT NULL,
                name VARCHAR(160) NOT NULL,
                market VARCHAR(8) NOT NULL,
                security_type VARCHAR(32) NOT NULL,
                listed_on DATE,
                delisted_on DATE,
                listing_status VARCHAR(32) NOT NULL,
                is_st BOOLEAN NOT NULL,
                is_suspended BOOLEAN NOT NULL,
                provider_codes JSON NOT NULL,
                master_version INTEGER NOT NULL,
                source VARCHAR(64) NOT NULL,
                source_version VARCHAR(160) NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
            """
        )
    SecurityUniverseSnapshot.__table__.create(engine)
    SecurityUniverseSnapshotItem.__table__.create(engine)
    security_id = uuid4()
    snapshot_id = uuid4()

    with Session(engine, expire_on_commit=False) as session:
        session.add(
            Security(
                id=security_id,
                symbol="600000.SH",
                exchange_code="600000",
                name="浦发银行",
                market="SH",
                security_type="A_SHARE",
                listing_status="SUSPENDED",
                listed_on=None,
                delisted_on=None,
                is_st=False,
                is_suspended=True,
                provider_codes={},
                master_version=1,
                source="eastmoney",
                source_version="v1",
            )
        )
        repository = SecurityRepository(AsyncSessionAdapter(session))
        frozen = SecurityUniverseSnapshot(
            id=snapshot_id,
            filters={"mode": "symbols", "symbols": ["600000.SH"]},
            item_count=1,
            master_version=1,
        )
        await repository.save_universe_snapshot(
            frozen,
            [
                SecurityUniverseSnapshotItem(
                    snapshot_id=snapshot_id,
                    symbol="600000.SH",
                    market="SH",
                    security_type="A_SHARE",
                    listing_status="SUSPENDED",
                    is_st=False,
                    is_suspended=True,
                    master_version=1,
                )
            ],
        )
        session.commit()

    with Session(engine) as session:
        current = session.get(Security, security_id)
        assert current is not None
        current.name = "更名后的浦发银行"
        current.listing_status = "DELISTED"
        current.is_suspended = False
        current.master_version = 2
        session.commit()

    with Session(engine) as session:
        reloaded = await SecurityRepository(
            AsyncSessionAdapter(session)
        ).get_universe_snapshot(snapshot_id)

        assert reloaded is not None
        assert reloaded is not frozen
        assert reloaded.item_count == 1
        assert reloaded.master_version == 1
        assert [
            (item.symbol, item.listing_status, item.master_version)
            for item in reloaded.items
        ] == [("600000.SH", "SUSPENDED", 1)]

    engine.dispose()


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
