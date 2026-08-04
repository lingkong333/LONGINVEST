import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from long_invest.modules.strategies.models import (
    StrategyScreeningBatch,
    StrategyScreeningPeriod,
    StrategyScreeningResult,
    StrategyScreeningScopeItem,
)
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.base import Base
from long_invest.platform.database.engine import Database

BACKEND = Path(__file__).parents[2]
HEAD_REVISION = "20260804_0042"
PREVIOUS_REVISION = "20260803_0040"
TABLES = {
    "strategy_screening_batch",
    "strategy_screening_period",
    "strategy_screening_scope_item",
    "strategy_screening_result",
}


def test_strategy_screening_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == [HEAD_REVISION]
    assert scripts.get_revision(HEAD_REVISION).down_revision == "20260804_0041"


def test_strategy_screening_models_are_registered() -> None:
    assert set(Base.metadata.tables) >= TABLES
    assert {
        StrategyScreeningBatch.__table__.name,
        StrategyScreeningPeriod.__table__.name,
        StrategyScreeningScopeItem.__table__.name,
        StrategyScreeningResult.__table__.name,
    } == TABLES


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    os.getenv("LONGINVEST_STRATEGY_SCREENING_MIGRATION_TESTS") != "1",
    reason="enable PostgreSQL strategy screening migration tests",
)
@pytest.mark.anyio
async def test_strategy_screening_migration_lifecycle_and_constraints() -> None:
    settings = AppSettings(_env_file=None)
    database_name = f"longinvest_screening_{uuid4().hex}"
    owner_base = make_url(settings.database_owner_url)
    maintenance = create_async_engine(
        owner_base.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    owner_url = owner_base.set(database=database_name)
    migration_env = os.environ.copy()
    migration_env["LONGINVEST_DATABASE_OWNER_URL"] = owner_url.render_as_string(
        hide_password=False
    )
    migration_env["LONGINVEST_DATABASE_URL"] = owner_url.render_as_string(
        hide_password=False
    )

    async with _temporary_database(maintenance, database_name):
        _run_alembic(migration_env, "upgrade", "head")
        await _assert_tables(owner_url, expected=True)
        identifiers = await _seed_screening(owner_url)
        await _assert_period_order_constraint(owner_url, identifiers)
        await _assert_result_shape_constraint(owner_url, identifiers)
        await _assert_idempotency_constraint(owner_url, identifiers)
        await _assert_completed_batch_immutable(owner_url, identifiers)
        _run_alembic(migration_env, "downgrade", PREVIOUS_REVISION)
        await _assert_tables(owner_url, expected=False)


@asynccontextmanager
async def _temporary_database(maintenance, database_name: str):
    try:
        async with maintenance.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield
    finally:
        async with maintenance.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await maintenance.dispose()


def _run_alembic(environment: dict[str, str], command: str, revision: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=BACKEND,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


async def _assert_tables(owner_url, *, expected: bool) -> None:
    database = Database(owner_url.render_as_string(hide_password=False))
    try:
        async with database.session() as session:
            revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            assert revision == (HEAD_REVISION if expected else PREVIOUS_REVISION)
            tables = await session.run_sync(
                lambda sync_session: set(
                    inspect(sync_session.connection()).get_table_names()
                )
            )
            assert (tables >= TABLES) is expected
            assert "backtest_task" in tables
    finally:
        await database.dispose()


async def _seed_screening(owner_url) -> dict[str, UUID]:
    identifiers = {
        name: uuid4()
        for name in (
            "strategy",
            "strategy_version",
            "universe",
            "security",
            "batch",
            "period_1",
            "period_2",
            "scope",
        )
    }
    database = Database(owner_url.render_as_string(hide_password=False))
    try:
        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO strategy (id, name, status) "
                    "VALUES (:id, 'T', 'DRAFT')"
                ),
                {"id": identifiers["strategy"]},
            )
            await session.execute(
                text(
                    "INSERT INTO strategy_version "
                    "(id, strategy_id, version_no, source_code_hash, source_code, "
                    "metadata, parameter_schema, environment_version, "
                    "runner_image_digest, status) VALUES "
                    "(:id, :strategy_id, 1, :hash, 'x', '{}'::jsonb, "
                    "'{}'::jsonb, 'py312', :digest, 'PUBLISHING')"
                ),
                {
                    "id": identifiers["strategy_version"],
                    "strategy_id": identifiers["strategy"],
                    "hash": "a" * 64,
                    "digest": f"sha256:{'b' * 64}",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO security_universe_snapshot "
                    "(id, filters, item_count, master_version) "
                    "VALUES (:id, '{}'::jsonb, 1, 1)"
                ),
                {"id": identifiers["universe"]},
            )
            await session.execute(
                text(
                    "INSERT INTO security "
                    "(id, symbol, exchange_code, name, market, security_type, "
                    "listing_status, provider_codes, master_version, source, "
                    "source_version) VALUES "
                    "(:id, '600000.SH', 'SH', 'T', 'SH', 'A_SHARE', 'LISTED', "
                    "'{}'::jsonb, 1, 'test', '1')"
                ),
                {"id": identifiers["security"]},
            )
            await session.execute(
                text(
                    "INSERT INTO strategy_screening_batch "
                    "(id, strategy_version_id, security_universe_snapshot_id, "
                    "parameter_snapshot, parameter_hash, request_hash, "
                    "idempotency_key, created_by_user_id, status) VALUES "
                    "(:id, :strategy_version_id, :universe_id, '{}'::jsonb, "
                    ":parameter_hash, :request_hash, 'screening-1', "
                    "'test-user', 'PENDING')"
                ),
                {
                    "id": identifiers["batch"],
                    "strategy_version_id": identifiers["strategy_version"],
                    "universe_id": identifiers["universe"],
                    "parameter_hash": "c" * 64,
                    "request_hash": "d" * 64,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO strategy_screening_scope_item "
                    "(id, batch_id, security_id, symbol, name) "
                    "VALUES (:id, :batch_id, :security_id, '600000.SH', 'T')"
                ),
                {
                    "id": identifiers["scope"],
                    "batch_id": identifiers["batch"],
                    "security_id": identifiers["security"],
                },
            )
            for period_id, sequence_no, dates in (
                (
                    identifiers["period_1"],
                    1,
                    (
                        date(2010, 1, 1),
                        date(2015, 12, 31),
                        date(2016, 1, 1),
                        date(2018, 12, 31),
                    ),
                ),
                (
                    identifiers["period_2"],
                    2,
                    (
                        date(2012, 1, 1),
                        date(2016, 12, 31),
                        date(2017, 1, 1),
                        date(2019, 12, 31),
                    ),
                ),
            ):
                await session.execute(
                    text(
                        "INSERT INTO strategy_screening_period "
                        "(id, batch_id, sequence_no, training_start_date, "
                        "training_end_date, test_start_date, test_end_date) "
                        "VALUES (:id, :batch_id, :sequence_no, :training_start, "
                        ":training_end, :test_start, :test_end)"
                    ),
                    {
                        "id": period_id,
                        "batch_id": identifiers["batch"],
                        "sequence_no": sequence_no,
                        "training_start": dates[0],
                        "training_end": dates[1],
                        "test_start": dates[2],
                        "test_end": dates[3],
                    },
                )
    finally:
        await database.dispose()
    return identifiers


async def _assert_period_order_constraint(owner_url, identifiers) -> None:
    database = Database(owner_url.render_as_string(hide_password=False))
    try:
        with pytest.raises(DBAPIError, match="must not move backward"):
            async with database.transaction() as session:
                await session.execute(
                    text(
                        "INSERT INTO strategy_screening_period "
                        "(id, batch_id, sequence_no, training_start_date, "
                        "training_end_date, test_start_date, test_end_date) "
                        "VALUES (:id, :batch_id, 3, '2011-01-01', '2017-12-31', "
                        "'2018-01-01', '2020-12-31')"
                    ),
                    {"id": uuid4(), "batch_id": identifiers["batch"]},
                )
    finally:
        await database.dispose()


async def _assert_result_shape_constraint(owner_url, identifiers) -> None:
    database = Database(owner_url.render_as_string(hide_password=False))
    statement = text(
        "INSERT INTO strategy_screening_result "
        "(id, batch_id, period_id, scope_item_id, status, low_strong, "
        "low_watch, high_watch, high_strong, training_data_hash, "
        "training_row_count, ended_at) VALUES "
        "(:id, :batch_id, :period_id, :scope_id, 'MATCHED', 8, 9, 12, "
        ":high_strong, :hash, 100, now())"
    )
    try:
        with pytest.raises(DBAPIError):
            async with database.transaction() as session:
                await session.execute(
                    statement,
                    {
                        "id": uuid4(),
                        "batch_id": identifiers["batch"],
                        "period_id": identifiers["period_1"],
                        "scope_id": identifiers["scope"],
                        "high_strong": None,
                        "hash": "e" * 64,
                    },
                )
        async with database.transaction() as session:
            await session.execute(
                statement,
                {
                    "id": uuid4(),
                    "batch_id": identifiers["batch"],
                    "period_id": identifiers["period_1"],
                    "scope_id": identifiers["scope"],
                    "high_strong": 13,
                    "hash": "e" * 64,
                },
            )
    finally:
        await database.dispose()


async def _assert_idempotency_constraint(owner_url, identifiers) -> None:
    database = Database(owner_url.render_as_string(hide_password=False))
    try:
        with pytest.raises(DBAPIError):
            async with database.transaction() as session:
                await session.execute(
                    text(
                        "INSERT INTO strategy_screening_batch "
                        "(id, strategy_version_id, security_universe_snapshot_id, "
                        "parameter_snapshot, parameter_hash, request_hash, "
                        "idempotency_key, created_by_user_id, status) VALUES "
                        "(:id, :version_id, :universe_id, '{}'::jsonb, :hash, "
                        ":request_hash, 'screening-1', 'test-user', 'PENDING')"
                    ),
                    {
                        "id": uuid4(),
                        "version_id": identifiers["strategy_version"],
                        "universe_id": identifiers["universe"],
                        "hash": "c" * 64,
                        "request_hash": "f" * 64,
                    },
                )
    finally:
        await database.dispose()


async def _assert_completed_batch_immutable(owner_url, identifiers) -> None:
    database = Database(owner_url.render_as_string(hide_password=False))
    try:
        async with database.transaction() as session:
            await session.execute(
                text(
                    "UPDATE strategy_screening_batch "
                    "SET status = 'SUCCEEDED', completed_at = now() WHERE id = :id"
                ),
                {"id": identifiers["batch"]},
            )
        with pytest.raises(DBAPIError, match="is immutable"):
            async with database.transaction() as session:
                await session.execute(
                    text(
                        "UPDATE strategy_screening_scope_item "
                        "SET name = 'changed' WHERE id = :id"
                    ),
                    {"id": identifiers["scope"]},
                )
    finally:
        await database.dispose()
