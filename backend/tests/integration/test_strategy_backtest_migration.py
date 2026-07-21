import ast
import os
import re
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
from sqlalchemy.schema import UniqueConstraint

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
EXPECTED_INDEXES = {
    "ix_strategy_status",
    "ix_strategy_validation_run_status",
    "ix_strategy_run_strategy_version_status",
    "ix_backtest_task_status_created",
    "ix_backtest_task_strategy_version",
    "ix_backtest_item_task_status",
    "ix_backtest_item_security",
    "ix_backtest_order_item_status",
    "ix_backtest_trade_item_execute_date",
    "ix_target_calculation_run_subscription_created",
    "ix_target_calculation_run_status",
    "ix_target_review_status_created",
    "ix_target_review_candidate",
}


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
    assert "ck_target_revision_source_valid" in source
    assert "ck_target_revision_source_revision_consistent" in source
    for index_name in EXPECTED_INDEXES:
        assert index_name in source


def test_new_constraint_and_index_names_are_globally_unique() -> None:
    relation_names: list[str] = list(TABLES)
    for table_name in TABLES:
        table = Base.metadata.tables[table_name]
        relation_names.extend(
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        relation_names.extend(index.name for index in table.indexes)

    assert len(relation_names) == len(set(relation_names))
    assert {
        index.name
        for table_name in TABLES
        for index in Base.metadata.tables[table_name].indexes
    } >= EXPECTED_INDEXES


def test_explicit_constraint_and_index_names_bypass_naming_convention() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    named_operations = {
        "create_check_constraint",
        "create_foreign_key",
        "create_index",
        "drop_constraint",
        "drop_index",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in named_operations:
            continue
        first_argument = node.args[0]
        assert isinstance(first_argument, ast.Call)
        assert isinstance(first_argument.func, ast.Attribute)
        assert first_argument.func.attr == "f"


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
    app_base = make_url(settings.database_url)
    maintenance_url = owner_base.set(database="postgres")
    owner_url = owner_base.set(database=database_name)
    app_url = app_base.set(database=database_name)
    migration_env = os.environ.copy()
    migration_env["LONGINVEST_DATABASE_OWNER_URL"] = owner_url.render_as_string(
        hide_password=False
    )
    migration_env["LONGINVEST_DATABASE_URL"] = app_url.render_as_string(
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
        await _assert_upgraded(owner_url, app_url=app_url, test_constraints=True)
        downgrade_failure = _run_alembic_failure(
            migration_env, "downgrade", PREVIOUS_REVISION
        )
        assert "stage4 tables contain data" in downgrade_failure.stderr


@pytest.mark.skipif(
    os.getenv("LONGINVEST_STRATEGY_BACKTEST_MIGRATION_TESTS") != "1",
    reason=(
        "set LONGINVEST_STRATEGY_BACKTEST_MIGRATION_TESTS=1 "
        "for PostgreSQL migration tests"
    ),
)
@pytest.mark.anyio
async def test_legacy_target_hashes_are_checked_before_upgrade() -> None:
    settings = AppSettings(_env_file=None)
    owner_base = make_url(settings.database_owner_url)
    for legacy_hash, succeeds in (("A" * 64, False), ("a" * 64, True)):
        database_name = f"longinvest_legacy_{uuid4().hex}"
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
        async with _temporary_database(maintenance, database_name):
            _run_alembic(migration_env, "upgrade", PREVIOUS_REVISION)
            await _insert_legacy_target(owner_url, legacy_hash)
            if succeeds:
                _run_alembic(migration_env, "upgrade", "head")
                await _assert_upgraded(owner_url)
            else:
                failure = _run_alembic_failure(migration_env, "upgrade", "head")
                assert "non-lowercase SHA-256 values" in failure.stderr


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


def _run_alembic_failure(
    environment: dict[str, str], command: str, revision: str
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=BACKEND,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode != 0
    return result


async def _insert_legacy_target(owner_url, content_hash: str) -> None:
    database = Database(owner_url.render_as_string(hide_password=False))
    security_id = uuid4()
    subscription_id = uuid4()
    try:
        async with database.transaction() as session:
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
            await session.execute(
                text(
                    "INSERT INTO target_revision "
                    "(id, subscription_id, revision_no, low_strong, low_watch, "
                    "high_watch, high_strong, source, target_date, "
                    "parameter_snapshot, content_hash, reason, "
                    "large_change_confirmed, request_id, idempotency_key, "
                    "actor_user_id, session_id, trusted_ip) VALUES "
                    "(:id, :subscription_id, 1, 8, 9, 12, 13, 'MANUAL', "
                    "CURRENT_DATE, '{}'::jsonb, :hash, 'test', false, "
                    "'request', 'legacy', 'user', 'session', '127.0.0.1')"
                ),
                {
                    "id": uuid4(),
                    "subscription_id": subscription_id,
                    "hash": content_hash,
                },
            )
    finally:
        await database.dispose()


async def _assert_upgraded(owner_url, *, app_url=None, test_constraints=False) -> None:
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
            await _assert_target_source_constraint_sql(session)

        if not test_constraints:
            return

        strategy_id = uuid4()
        strategy_version_id = uuid4()
        validation_run_id = uuid4()
        security_id = uuid4()
        subscription_id = uuid4()
        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO strategy (id, name, status) VALUES (:id, 'T', 'DRAFT')"
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
            await session.execute(
                text(
                    "INSERT INTO strategy_validation_run "
                    "(id, strategy_id, strategy_version_id, draft_version, "
                    "source_code_hash, evidence_snapshot, status, completed_at) "
                    "VALUES (:id, :strategy_id, :strategy_version_id, 1, :hash, "
                    '\'{"checks":["static","sandbox"]}\'::jsonb, '
                    "'SUCCEEDED', now())"
                ),
                {
                    "id": validation_run_id,
                    "strategy_id": strategy_id,
                    "strategy_version_id": strategy_version_id,
                    "hash": "a" * 64,
                },
            )
            await session.execute(
                text(
                    "UPDATE strategy_version SET status = 'PUBLISHED', "
                    "validation_run_id = :validation_run_id, "
                    "git_commit = :git_commit, published_at = now() "
                    "WHERE id = :id"
                ),
                {
                    "id": strategy_version_id,
                    "validation_run_id": validation_run_id,
                    "git_commit": "c" * 40,
                },
            )

        mismatched_version_id = uuid4()
        mismatched_validation_id = uuid4()
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
                    "id": mismatched_version_id,
                    "strategy_id": strategy_id,
                    "hash": "d" * 64,
                    "digest": f"sha256:{'a' * 64}",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO strategy_validation_run "
                    "(id, strategy_id, strategy_version_id, draft_version, "
                    "source_code_hash, evidence_snapshot, status, completed_at) "
                    "VALUES (:id, :strategy_id, :strategy_version_id, 1, :hash, "
                    "'{}'::jsonb, 'SUCCEEDED', now())"
                ),
                {
                    "id": mismatched_validation_id,
                    "strategy_id": strategy_id,
                    "strategy_version_id": mismatched_version_id,
                    "hash": "e" * 64,
                },
            )

        with pytest.raises(DBAPIError, match="matching validation evidence"):
            async with database.transaction() as session:
                await session.execute(
                    text(
                        "UPDATE strategy_version SET status = 'PUBLISHED', "
                        "validation_run_id = :validation_run_id, "
                        "git_commit = :git_commit, published_at = now() "
                        "WHERE id = :id"
                    ),
                    {
                        "id": mismatched_version_id,
                        "validation_run_id": mismatched_validation_id,
                        "git_commit": "f" * 40,
                    },
                )

        with pytest.raises(DBAPIError, match="completed strategy validation"):
            async with database.transaction() as session:
                await session.execute(
                    text(
                        "UPDATE strategy_validation_run "
                        "SET evidence_snapshot = CAST(:evidence AS jsonb) "
                        "WHERE id = :id"
                    ),
                    {"id": validation_run_id, "evidence": '{"tampered":true}'},
                )

        valid_target = (
            "INSERT INTO target_revision "
            "(id, subscription_id, revision_no, low_strong, low_watch, "
            "high_watch, high_strong, source, target_date, strategy_version_id, "
            "parameter_snapshot, content_hash, reason, large_change_confirmed, "
            "request_id, idempotency_key, actor_user_id, session_id, trusted_ip) "
            "VALUES (:id, :subscription_id, :revision, 8, 9, 12, 13, :source, "
            "CURRENT_DATE, :strategy_version_id, '{}'::jsonb, :hash, 'test', "
            "false, 'request', :idempotency_key, 'user', 'session', '127.0.0.1')"
        )
        async with database.transaction() as session:
            await session.execute(
                text(valid_target),
                {
                    "id": uuid4(),
                    "subscription_id": subscription_id,
                    "revision": 10,
                    "source": "STRATEGY",
                    "strategy_version_id": strategy_version_id,
                    "hash": "b" * 64,
                    "idempotency_key": "valid-strategy",
                },
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

        assert app_url is not None
        application = Database(app_url.render_as_string(hide_password=False))
        try:
            async with application.session() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT has_table_privilege(current_user, "
                            "'strategy_version', 'UPDATE')"
                        )
                    )
                    is True
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT has_table_privilege(current_user, "
                            "'backtest_forecast_snapshot', 'UPDATE')"
                        )
                    )
                    is False
                )
            with pytest.raises(DBAPIError) as immutable_version_error:
                async with application.transaction() as session:
                    await session.execute(
                        text(
                            "UPDATE strategy_version SET source_code = 'changed' "
                            "WHERE id = :id"
                        ),
                        {"id": strategy_version_id},
                    )
            assert "published strategy version facts are immutable" in str(
                immutable_version_error.value.orig
            )
        finally:
            await application.dispose()
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

        expected_indexes = {
            (index.name, tuple(column.name for column in index.columns))
            for index in model_table.indexes
        }
        actual_indexes = {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes(table_name)
            if "duplicates_constraint" not in index
        }
        assert actual_indexes == expected_indexes

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
                (foreign_key["referred_table"],) * len(foreign_key["referred_columns"]),
                tuple(foreign_key["referred_columns"]),
                foreign_key["options"].get("ondelete"),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        assert actual_foreign_keys == expected_foreign_keys


async def _assert_target_source_constraint_sql(session) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid, true) AS definition "
                "FROM pg_constraint WHERE conrelid = 'target_revision'::regclass "
                "AND conname IN ('ck_target_revision_source_valid', "
                "'ck_target_revision_source_revision_consistent')"
            )
        )
    ).mappings()
    definitions = {row["conname"]: row["definition"] for row in rows}

    expected_source_sql = next(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables["target_revision"].constraints
        if constraint.name == "ck_target_revision_source_valid"
    )
    expected_sources = set(re.findall(r"'([A-Z_]+)'", expected_source_sql))
    actual_sources = set(
        re.findall(
            r"'([A-Z_]+)'",
            definitions["ck_target_revision_source_valid"],
        )
    )
    assert actual_sources == expected_sources

    revision_definition = definitions[
        "ck_target_revision_source_revision_consistent"
    ].lower()
    assert revision_definition.count("restored") == 2
    assert "source_revision_id is not null" in revision_definition
    assert "source_revision_id is null" in revision_definition
