import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from long_invest.modules.backtests.models import BacktestTask
from long_invest.modules.strategies.models import Strategy
from long_invest.modules.targets.models import TargetCalculationRun, TargetReview
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.base import Base
from long_invest.platform.database.engine import Database

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic" / "versions" / "20260721_0012_strategy_backtest.py"
ALEMBIC_ENV = BACKEND / "alembic" / "env.py"
REVISION = "20260721_0012"
PREVIOUS_REVISION = "20260717_0011"
TABLES = (
    "strategy",
    "strategy_draft",
    "strategy_draft_revision",
    "strategy_validation_run",
    "strategy_version",
    "strategy_run",
    "backtest_task",
    "backtest_universe_snapshot",
    "backtest_item",
    "backtest_forecast_snapshot",
    "backtest_target_adjustment",
    "backtest_order",
    "backtest_trade",
    "backtest_metric",
    "backtest_daily_result",
    "target_calculation_run",
    "target_review",
)


def test_strategy_backtest_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))

    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]


def test_strategy_backtest_models_are_registered_with_alembic_metadata() -> None:
    source = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert "long_invest.modules.strategies.models" in source
    assert "long_invest.modules.backtests.models" in source
    assert "TargetCalculationRun" in source
    assert "TargetReview" in source
    assert {
        Strategy.__table__.name,
        BacktestTask.__table__.name,
        TargetCalculationRun.__table__.name,
        TargetReview.__table__.name,
    } <= set(Base.metadata.tables)


def test_strategy_backtest_migration_declares_all_tables_and_constraints() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert f'revision: str = "{REVISION}"' in source
    assert f'down_revision: str | None = "{PREVIOUS_REVISION}"' in source
    for table in TABLES:
        assert f'"{table}"' in source
    for constraint in (
        "ck_strategy_version_source_code_hash_sha256",
        "ck_backtest_task_hashes_sha256",
        "ck_backtest_forecast_snapshot_targets_ordered",
        "ck_backtest_metric_win_rate_consistent",
        "ck_target_revision_strategy_version_consistent",
        "fk_target_revision_strategy_version_id_strategy_version",
    ):
        assert constraint in source


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    os.getenv("LONGINVEST_STRATEGY_BACKTEST_MIGRATION_TESTS") != "1",
    reason=(
        "set LONGINVEST_STRATEGY_BACKTEST_MIGRATION_TESTS=1 "
        "for PostgreSQL migration tests"
    ),
)
@pytest.mark.anyio
async def test_strategy_backtest_migration_lifecycle_and_constraints() -> None:
    settings = AppSettings(_env_file=None)
    database_name = f"longinvest_sb_{uuid4().hex}"
    owner_base = make_url(settings.database_owner_url)
    maintenance_url = owner_base.set(database="postgres")
    owner_url = owner_base.set(database=database_name)
    migration_env = os.environ.copy()
    migration_env["LONGINVEST_DATABASE_OWNER_URL"] = owner_url.render_as_string(
        hide_password=False
    )

    maintenance = create_async_engine(
        maintenance_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    async with _temporary_database(maintenance, database_name):
        _run_alembic(migration_env, "upgrade", "head")
        await _assert_upgraded(owner_url)
        _run_alembic(migration_env, "downgrade", PREVIOUS_REVISION)
        await _assert_downgraded(owner_url)
        _run_alembic(migration_env, "upgrade", "head")
        await _assert_upgraded(owner_url)


@asynccontextmanager
async def _temporary_database(maintenance, database_name: str):
    try:
        async with maintenance.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield
    finally:
        async with maintenance.connect() as connection:
            await connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = :database_name AND pid <> pg_backend_pid()"),
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


async def _assert_upgraded(owner_url) -> None:
    database = Database(owner_url.render_as_string(hide_password=False))
    try:
        async with database.session() as session:
            current_revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            assert current_revision == REVISION
            existing = await session.run_sync(
                lambda sync_session: set(
                    inspect(sync_session.connection()).get_table_names()
                )
            )
            assert set(TABLES) <= existing
            await session.run_sync(_assert_schema_matches_models)

        strategy_id = uuid4()
        strategy_version_id = uuid4()
        security_id = uuid4()
        subscription_id = uuid4()
        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO strategy (id, name, status) "
                    "VALUES (:id, 'T', 'DRAFT')"
                ),
                {"id": strategy_id},
            )
            await session.execute(
                text(
                    "INSERT INTO strategy_version "
                    "(id, strategy_id, version_no, source_code_hash, source_code, "
                    "metadata, parameter_schema, environment_version, "
                    "runner_image_digest, status) VALUES "
                    "(:id, :strategy_id, 1, :hash, 'x', '{}'::jsonb, '{}'::jsonb, "
                    "'py312', :digest, 'PUBLISHING')"
                ),
                {
                    "id": strategy_version_id,
                    "strategy_id": strategy_id,
                    "hash": "a" * 64,
                    "digest": f"sha256:{'a' * 64}",
                },
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
                {"id": security_id},
            )
            await session.execute(
                text(
                    "INSERT INTO monitor_subscription "
                    "(id, security_id, symbol, status, version) VALUES "
                    "(:id, :security_id, '600000.SH', 'CONFIGURING', 1)"
                ),
                {"id": subscription_id, "security_id": security_id},
            )

        with pytest.raises(DBAPIError) as invalid_hash_error:
            async with database.transaction() as session:
                await session.execute(
                    text(
                        "INSERT INTO strategy_version "
                        "(id, strategy_id, version_no, source_code_hash, source_code, "
                        "metadata, parameter_schema, environment_version, "
                        "runner_image_digest, status) VALUES "
                        "(:id, :strategy_id, 2, :hash, 'x', '{}'::jsonb, '{}'::jsonb, "
                        "'py312', :digest, 'PUBLISHING')"
                    ),
                    {
                        "id": uuid4(),
                        "strategy_id": strategy_id,
                        "hash": "G" * 64,
                        "digest": f"sha256:{'a' * 64}",
                    },
                )
        assert "ck_strategy_version_source_code_hash_sha256" in str(
            invalid_hash_error.value.orig
        )

        invalid_target = (
            "INSERT INTO target_revision "
            "(id, subscription_id, revision_no, low_strong, low_watch, "
            "high_watch, high_strong, source, target_date, strategy_version_id, "
            "parameter_snapshot, content_hash, reason, large_change_confirmed, "
            "request_id, idempotency_key, actor_user_id, session_id, trusted_ip) "
            "VALUES (:id, :subscription_id, :revision, 8, 9, 12, 13, :source, "
            "CURRENT_DATE, :strategy_version_id, '{}'::jsonb, :hash, 'test', "
            "false, 'request', :idempotency_key, 'user', 'session', '127.0.0.1')"
        )
        with pytest.raises(DBAPIError) as dangling_strategy_error:
            async with database.transaction() as session:
                await session.execute(
                    text(invalid_target),
                    {
                        "id": uuid4(),
                        "subscription_id": subscription_id,
                        "revision": 1,
                        "source": "STRATEGY",
                        "strategy_version_id": uuid4(),
                        "hash": "b" * 64,
                        "idempotency_key": "dangling-strategy",
                    },
                )
        assert "fk_target_revision_strategy_version_id_strategy_version" in str(
            dangling_strategy_error.value.orig
        )

        with pytest.raises(DBAPIError) as inconsistent_source_error:
            async with database.transaction() as session:
                await session.execute(
                    text(invalid_target),
                    {
                        "id": uuid4(),
                        "subscription_id": subscription_id,
                        "revision": 2,
                        "source": "MANUAL",
                        "strategy_version_id": strategy_version_id,
                        "hash": "b" * 64,
                        "idempotency_key": "inconsistent-source",
                    },
                )
        assert "ck_target_revision_strategy_version_consistent" in str(
            inconsistent_source_error.value.orig
        )

        with pytest.raises(DBAPIError) as orphan_version_error:
            async with database.transaction() as session:
                await session.execute(
                    text(
                        "INSERT INTO strategy_version "
                        "(id, strategy_id, version_no, source_code_hash, source_code, "
                        "metadata, parameter_schema, environment_version, "
                        "runner_image_digest, status) VALUES "
                        "(:id, :strategy_id, 2, :hash, 'x', '{}'::jsonb, '{}'::jsonb, "
                        "'py312', :digest, 'PUBLISHING')"
                    ),
                    {
                        "id": uuid4(),
                        "strategy_id": uuid4(),
                        "hash": "a" * 64,
                        "digest": f"sha256:{'a' * 64}",
                    },
                )
        assert "fk_strategy_version_strategy_id_strategy" in str(
            orphan_version_error.value.orig
        )
    finally:
        await database.dispose()


async def _assert_downgraded(owner_url) -> None:
    database = Database(owner_url.render_as_string(hide_password=False))
    try:
        async with database.session() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == PREVIOUS_REVISION
            )
            existing = await session.run_sync(
                lambda sync_session: set(
                    inspect(sync_session.connection()).get_table_names()
                )
            )
            assert set(TABLES).isdisjoint(existing)
    finally:
        await database.dispose()


def _assert_schema_matches_models(sync_session) -> None:
    inspector = inspect(sync_session.connection())
    for table_name in TABLES:
        model_table = Base.metadata.tables[table_name]
        actual_columns = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        assert set(actual_columns) == {column.name for column in model_table.columns}
        for column in model_table.columns:
            actual = actual_columns[column.name]
            assert actual["nullable"] is column.nullable
            assert (actual["default"] is not None) is (
                column.server_default is not None
            )

        expected_checks = {
            constraint.name
            for constraint in model_table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        actual_checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
        }
        assert actual_checks == expected_checks

        expected_unique_constraints = {
            (constraint.name, tuple(column.name for column in constraint.columns))
            for constraint in model_table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        actual_unique_constraints = {
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(table_name)
        }
        assert actual_unique_constraints == expected_unique_constraints

        expected_foreign_keys = {
            (
                tuple(foreign_key.parent.name for foreign_key in constraint.elements),
                tuple(
                    foreign_key.target_fullname.split(".", maxsplit=1)[0]
                    for foreign_key in constraint.elements
                ),
                tuple(
                    foreign_key.target_fullname.split(".", maxsplit=1)[1]
                    for foreign_key in constraint.elements
                ),
                constraint.ondelete,
            )
            for constraint in model_table.foreign_key_constraints
        }
        actual_foreign_keys = {
            (
                tuple(foreign_key["constrained_columns"]),
                (foreign_key["referred_table"],) * len(
                    foreign_key["referred_columns"]
                ),
                tuple(foreign_key["referred_columns"]),
                foreign_key["options"].get("ondelete"),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        assert actual_foreign_keys == expected_foreign_keys
