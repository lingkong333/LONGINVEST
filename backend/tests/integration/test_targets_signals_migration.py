import os
import re
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from long_invest.modules.monitoring.models import MonitorSubscription  # noqa: F401
from long_invest.modules.signals.models import (
    SignalEvaluation,
    SignalEvent,
    SignalState,
)
from long_invest.modules.targets.models import SubscriptionTargetBinding, TargetRevision
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.base import Base
from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.models import Job  # noqa: F401

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic" / "versions" / "20260717_0011_targets_signals.py"
ALEMBIC_ENV = BACKEND / "alembic" / "env.py"
TABLES = (
    "target_revision",
    "subscription_target_binding",
    "signal_state",
    "signal_evaluation",
    "signal_event",
)
IMMUTABLE_TABLES = {"target_revision", "signal_evaluation", "signal_event"}
MUTABLE_TABLES = {"subscription_target_binding", "signal_state"}
MODEL_TABLES = (
    TargetRevision.__table__,
    SubscriptionTargetBinding.__table__,
    SignalState.__table__,
    SignalEvaluation.__table__,
    SignalEvent.__table__,
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_targets_signals_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))

    assert ScriptDirectory.from_config(config).get_heads() == ["20260717_0011"]


def test_targets_signals_models_are_registered_with_metadata_and_alembic() -> None:
    source = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert "long_invest.modules.targets.models" in source
    assert "long_invest.modules.signals.models" in source
    assert {
        TargetRevision.__table__.name,
        SubscriptionTargetBinding.__table__.name,
        SignalState.__table__.name,
        SignalEvaluation.__table__.name,
        SignalEvent.__table__.name,
    } <= set(Base.metadata.tables)


def test_targets_signals_migration_matches_revision_and_table_order() -> None:
    source = _source()

    assert 'revision: str = "20260717_0011"' in source
    assert 'down_revision: str | None = "20260716_0010"' in source
    creates = [source.index(f'op.create_table(\n        "{table}"') for table in TABLES]
    assert creates == sorted(creates)
    assert "target_calculation_run" not in source
    assert "target_review" not in source


def test_targets_signals_migration_has_model_constraints_and_indexes() -> None:
    source = _source()

    for constraint in (
        "ck_target_revision_values_ordered",
        "ck_target_revision_source_revision_consistent",
        "ck_subscription_target_binding_status_valid",
        "ck_signal_state_last_inputs_valid",
        "ck_signal_evaluation_non_skipped_inputs_complete",
        "ck_signal_evaluation_target_values_valid",
        "ck_signal_event_real_transition",
        "ck_signal_event_target_values_valid",
    ):
        assert constraint in source
    for index in (
        "ix_target_revision_subscription_created",
        "ix_subscription_target_binding_status",
        "ix_signal_evaluation_subscription_created",
        "ix_signal_event_subscription_created",
        "ix_signal_event_notification_eligible",
    ):
        assert index in source


def test_targets_signals_migration_applies_least_privilege_and_immutability() -> None:
    source = _source()

    assert "database_app_role" in source
    assert 'r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"' in source
    assert "GRANT SELECT, INSERT ON TABLE" in source
    assert "GRANT UPDATE ON TABLE" in source
    for table in IMMUTABLE_TABLES:
        assert f'"{table}"' in source.split("IMMUTABLE_TABLES =", maxsplit=1)[1]
    assert 'f"CREATE TRIGGER {table_name}_append_only "' in source


def test_targets_signals_migration_downgrades_in_reverse_order() -> None:
    downgrade = _source().split("def downgrade() -> None:", maxsplit=1)[1]
    drops = [downgrade.index(f'op.drop_table("{table}")') for table in reversed(TABLES)]

    assert drops == sorted(drops)
    assert downgrade.index("DROP TRIGGER") < min(drops)
    assert downgrade.index("DROP FUNCTION") < min(drops)


@pytest.mark.parametrize(
    "mutation",
    ("missing_column", "nullable_changed", "foreign_key_missing", "default_missing"),
)
def test_schema_checker_rejects_structural_drift(mutation: str) -> None:
    expected = {
        "sample": {
            "columns": {
                "id": {
                    "type": ("uuid",),
                    "nullable": False,
                    "server_default": True,
                }
            },
            "foreign_keys": {
                (("id",), "parent", ("id",), "RESTRICT"),
            },
            "unique_constraints": set(),
            "check_constraints": {},
            "indexes": set(),
        }
    }
    actual = deepcopy(expected)

    if mutation == "missing_column":
        actual["sample"]["columns"].pop("id")
    elif mutation == "nullable_changed":
        actual["sample"]["columns"]["id"]["nullable"] = True
    elif mutation == "foreign_key_missing":
        actual["sample"]["foreign_keys"].clear()
    else:
        actual["sample"]["columns"]["id"]["server_default"] = False

    with pytest.raises(AssertionError):
        _assert_schema_snapshot(expected, actual)


def test_model_schema_snapshot_covers_every_target_and_signal_column() -> None:
    snapshot = _expected_model_schema()

    assert set(snapshot) == set(TABLES)
    for table in MODEL_TABLES:
        assert set(snapshot[table.name]["columns"]) == set(table.columns.keys())
        assert snapshot[table.name]["primary_key"] == ("id",)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    os.getenv("LONGINVEST_TARGET_SIGNAL_MIGRATION_TESTS") != "1",
    reason=(
        "set LONGINVEST_TARGET_SIGNAL_MIGRATION_TESTS=1 for PostgreSQL migration tests"
    ),
)
@pytest.mark.anyio
async def test_targets_signals_schema_and_application_role_privileges() -> None:
    settings = AppSettings(_env_file=None)
    owner = Database(settings.database_owner_url)
    application = Database(settings.database_url)
    try:
        async with owner.session() as session:
            schema = await session.run_sync(_inspect_schema)
            _assert_schema_snapshot(_expected_model_schema(), schema)
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "20260717_0011"
            )

        async with application.session() as session:
            for table in TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    allowed = await session.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "current_user, :table, :privilege)"
                        ),
                        {"table": table, "privilege": privilege},
                    )
                    expected = privilege in {"SELECT", "INSERT"} or (
                        privilege == "UPDATE" and table in MUTABLE_TABLES
                    )
                    assert allowed is expected
    finally:
        await owner.dispose()
        await application.dispose()


@pytest.mark.skipif(
    os.getenv("LONGINVEST_TARGET_SIGNAL_MIGRATION_TESTS") != "1",
    reason=(
        "set LONGINVEST_TARGET_SIGNAL_MIGRATION_TESTS=1 for PostgreSQL migration tests"
    ),
)
@pytest.mark.anyio
async def test_targets_signals_migration_lifecycle_in_temporary_database() -> None:
    settings = AppSettings(_env_file=None)
    database_name = f"longinvest_ts_{uuid4().hex}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)

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
    created = False
    try:
        async with maintenance.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
            created = True

        _run_alembic(migration_env, "upgrade", "head")
        await _assert_upgraded_database(owner_url, app_url)

        _run_alembic(migration_env, "downgrade", "20260716_0010")
        await _assert_downgraded_database(owner_url)

        _run_alembic(migration_env, "upgrade", "head")
        await _assert_upgraded_database(owner_url, app_url)
    finally:
        if created:
            async with maintenance.connect() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname = :database_name "
                        "AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                await connection.execute(text(f'DROP DATABASE "{database_name}"'))
        await maintenance.dispose()


@pytest.mark.skipif(
    os.getenv("LONGINVEST_TARGET_SIGNAL_MIGRATION_TESTS") != "1",
    reason=(
        "set LONGINVEST_TARGET_SIGNAL_MIGRATION_TESTS=1 for PostgreSQL migration tests"
    ),
)
@pytest.mark.anyio
async def test_targets_signals_application_crud_and_append_only_triggers() -> None:
    settings = AppSettings(_env_file=None)
    owner = Database(settings.database_owner_url)
    application = Database(settings.database_url)
    security_id = uuid4()
    subscription_id = uuid4()
    target_id = uuid4()
    binding_id = uuid4()
    state_id = uuid4()
    evaluation_id = uuid4()
    event_id = uuid4()
    token = uuid4().hex
    now = datetime.now(UTC)
    try:
        async with owner.transaction() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO security
                        (id, symbol, exchange_code, name, market, security_type,
                         listing_status, is_st, is_suspended, provider_codes,
                         master_version, source, source_version)
                    VALUES
                        (:id, :symbol, 'SSE', 'migration fixture', 'SH', 'A_SHARE',
                         'LISTED', false, false, '{}'::jsonb, 1, 'test', :token)
                    """
                ),
                {"id": security_id, "symbol": f"T{token[:8]}", "token": token},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO monitor_subscription
                        (id, security_id, symbol, status, version)
                    VALUES (:id, :security_id, :symbol, 'CONFIGURING', 1)
                    """
                ),
                {
                    "id": subscription_id,
                    "security_id": security_id,
                    "symbol": f"T{token[:8]}",
                },
            )

        async with application.transaction() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO target_revision
                        (id, subscription_id, revision_no, low_strong, low_watch,
                         high_watch, high_strong, source, target_date,
                         parameter_snapshot, content_hash, reason,
                         large_change_confirmed, request_id, idempotency_key,
                         actor_user_id, session_id, trusted_ip)
                    VALUES
                        (:id, :subscription_id, 1, 10, 20, 30, 40, 'MANUAL',
                         :target_date, '{}'::jsonb, :hash, 'fixture', false,
                         :token, :token, :token, :token, '127.0.0.1')
                    """
                ),
                {
                    "id": target_id,
                    "subscription_id": subscription_id,
                    "target_date": date.today(),
                    "hash": "a" * 64,
                    "token": token,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO subscription_target_binding
                        (id, subscription_id, current_revision_id, status, version,
                         activated_at)
                    VALUES (:id, :subscription_id, :target_id, 'READY', 1, :now)
                    """
                ),
                {
                    "id": binding_id,
                    "subscription_id": subscription_id,
                    "target_id": target_id,
                    "now": now,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO signal_evaluation
                        (id, subscription_id, idempotency_key, reason, result,
                         before_zone, after_zone, subscription_version,
                         target_revision_id, target_version, target_date,
                         low_strong, low_watch, high_watch, high_strong,
                         position_status, position_version, price, price_at,
                         price_version, hysteresis_applied, used_stale_target,
                         content_hash)
                    VALUES
                        (:id, :subscription_id, :token, 'MANUAL_CHECK', 'APPLIED',
                         'UNKNOWN', 'LOW', 1, :target_id, 1, :target_date,
                         10, 20, 30, 40, 'NOT_HOLDING', 0, 15, :now, 1,
                         false, false, :hash)
                    """
                ),
                {
                    "id": evaluation_id,
                    "subscription_id": subscription_id,
                    "token": token,
                    "target_id": target_id,
                    "target_date": date.today(),
                    "now": now,
                    "hash": "b" * 64,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO signal_event
                        (id, subscription_id, evaluation_id, before_zone,
                         after_zone, reason, price, price_at, target_revision_id,
                         target_version, target_date, low_strong, low_watch,
                         high_watch, high_strong, position_status, position_version,
                         used_stale_target, state_version, notification_class,
                         notification_eligible)
                    VALUES
                        (:id, :subscription_id, :evaluation_id, 'UNKNOWN', 'LOW',
                         'MANUAL_CHECK', 15, :now, :target_id, 1, :target_date,
                         10, 20, 30, 40, 'NOT_HOLDING', 0, false, 1, 'LOW', true)
                    """
                ),
                {
                    "id": event_id,
                    "subscription_id": subscription_id,
                    "evaluation_id": evaluation_id,
                    "now": now,
                    "target_id": target_id,
                    "target_date": date.today(),
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO signal_state
                        (id, subscription_id, zone, version, last_price,
                         last_price_at, last_subscription_version,
                         last_price_version, last_target_revision_id,
                         last_target_version, last_position_version,
                         last_evaluation_id, last_event_id)
                    VALUES
                        (:id, :subscription_id, 'LOW', 1, 15, :now, 1, 1,
                         :target_id, 1, 0, :evaluation_id, :event_id)
                    """
                ),
                {
                    "id": state_id,
                    "subscription_id": subscription_id,
                    "now": now,
                    "target_id": target_id,
                    "evaluation_id": evaluation_id,
                    "event_id": event_id,
                },
            )
            await session.execute(
                text(
                    "UPDATE subscription_target_binding SET version = 2 WHERE id = :id"
                ),
                {"id": binding_id},
            )
            await session.execute(
                text("UPDATE signal_state SET version = 2 WHERE id = :id"),
                {"id": state_id},
            )
            count = await session.scalar(
                text(
                    "SELECT count(*) FROM signal_event "
                    "WHERE subscription_id = :subscription_id"
                ),
                {"subscription_id": subscription_id},
            )
            assert count == 1

        for table, row_id in (
            ("target_revision", target_id),
            ("signal_evaluation", evaluation_id),
            ("signal_event", event_id),
        ):
            for statement in (
                f"UPDATE {table} SET id = id WHERE id = :id",
                f"DELETE FROM {table} WHERE id = :id",
            ):
                async with owner.session() as session:
                    with pytest.raises(DBAPIError):
                        await session.execute(text(statement), {"id": row_id})
                    await session.rollback()
    finally:
        try:
            async with owner.transaction() as session:
                for table in IMMUTABLE_TABLES:
                    await session.execute(
                        text(f"ALTER TABLE {table} DISABLE TRIGGER USER")
                    )
                await session.execute(
                    text("DELETE FROM signal_event WHERE id = :id"), {"id": event_id}
                )
                await session.execute(
                    text("DELETE FROM signal_state WHERE id = :id"), {"id": state_id}
                )
                await session.execute(
                    text("DELETE FROM signal_evaluation WHERE id = :id"),
                    {"id": evaluation_id},
                )
                await session.execute(
                    text("DELETE FROM subscription_target_binding WHERE id = :id"),
                    {"id": binding_id},
                )
                await session.execute(
                    text("DELETE FROM target_revision WHERE id = :id"),
                    {"id": target_id},
                )
                await session.execute(
                    text("DELETE FROM monitor_subscription WHERE id = :id"),
                    {"id": subscription_id},
                )
                await session.execute(
                    text("DELETE FROM security WHERE id = :id"), {"id": security_id}
                )
                for table in IMMUTABLE_TABLES:
                    await session.execute(
                        text(f"ALTER TABLE {table} ENABLE TRIGGER USER")
                    )
        finally:
            await owner.dispose()
            await application.dispose()


def _inspect_schema(sync_session):
    inspector = inspect(sync_session.connection())
    snapshot = {}
    for table_name in TABLES:
        columns = inspector.get_columns(table_name)
        snapshot[table_name] = {
            "columns": {
                column["name"]: {
                    "type": _type_signature(column["type"]),
                    "nullable": column["nullable"],
                    "server_default": column.get("default") is not None,
                }
                for column in columns
            },
            "foreign_keys": {
                (
                    tuple(item["constrained_columns"]),
                    item["referred_table"],
                    tuple(item["referred_columns"]),
                    item.get("options", {}).get("ondelete"),
                )
                for item in inspector.get_foreign_keys(table_name)
            },
            "primary_key": tuple(
                inspector.get_pk_constraint(table_name)["constrained_columns"]
            ),
            "unique_constraints": {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints(table_name)
            },
            "check_constraints": {
                item["name"]: _check_signature(item["sqltext"], columns)
                for item in inspector.get_check_constraints(table_name)
            },
            "indexes": {
                (item["name"], tuple(item["column_names"]), item["unique"])
                for item in inspector.get_indexes(table_name)
                if not item.get("duplicates_constraint")
            },
        }
    return snapshot


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


async def _assert_upgraded_database(owner_url, app_url) -> None:
    owner = Database(owner_url.render_as_string(hide_password=False))
    application = Database(app_url.render_as_string(hide_password=False))
    try:
        async with owner.session() as session:
            current_revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            assert current_revision == "20260717_0011"
            _assert_schema_snapshot(
                _expected_model_schema(), await session.run_sync(_inspect_schema)
            )
            triggers = set(
                (
                    await session.execute(
                        text(
                            "SELECT trigger_name FROM information_schema.triggers "
                            "WHERE event_object_table = ANY(:tables)"
                        ),
                        {"tables": list(TABLES)},
                    )
                ).scalars()
            )
            assert triggers == {
                f"{table_name}_append_only" for table_name in IMMUTABLE_TABLES
            }
            function_count = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_proc "
                    "WHERE proname = 'reject_target_signal_fact_mutation'"
                )
            )
            assert function_count == 1

        async with application.session() as session:
            await _assert_application_privileges(session)
    finally:
        await owner.dispose()
        await application.dispose()


async def _assert_downgraded_database(owner_url) -> None:
    owner = Database(owner_url.render_as_string(hide_password=False))
    try:
        async with owner.session() as session:
            current_revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            assert current_revision == "20260716_0010"
            existing_tables = await session.run_sync(
                lambda sync_session: set(
                    inspect(sync_session.connection()).get_table_names()
                )
            )
            assert set(TABLES).isdisjoint(existing_tables)
            trigger_count = await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.triggers "
                    "WHERE trigger_name LIKE '%_append_only' "
                    "AND event_object_table = ANY(:tables)"
                ),
                {"tables": list(TABLES)},
            )
            assert trigger_count == 0
            function_count = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_proc "
                    "WHERE proname = 'reject_target_signal_fact_mutation'"
                )
            )
            assert function_count == 0
    finally:
        await owner.dispose()


async def _assert_application_privileges(session) -> None:
    for table in TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            allowed = await session.scalar(
                text("SELECT has_table_privilege(current_user, :table, :privilege)"),
                {"table": table, "privilege": privilege},
            )
            expected = privilege in {"SELECT", "INSERT"} or (
                privilege == "UPDATE" and table in MUTABLE_TABLES
            )
            assert allowed is expected


def _expected_model_schema():
    snapshot = {}
    for table in MODEL_TABLES:
        columns = list(table.columns)
        snapshot[table.name] = {
            "columns": {
                column.name: {
                    "type": _type_signature(column.type),
                    "nullable": column.nullable,
                    "server_default": column.server_default is not None,
                }
                for column in columns
            },
            "foreign_keys": {
                (
                    tuple(column.name for column in constraint.columns),
                    constraint.referred_table.name,
                    tuple(element.column.name for element in constraint.elements),
                    constraint.ondelete,
                )
                for constraint in table.foreign_key_constraints
            },
            "primary_key": tuple(column.name for column in table.primary_key.columns),
            "unique_constraints": {
                tuple(column.name for column in constraint.columns)
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
            },
            "check_constraints": {
                constraint.name: _check_signature(str(constraint.sqltext), columns)
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            },
            "indexes": {
                (
                    index.name,
                    tuple(column.name for column in index.columns),
                    index.unique,
                )
                for index in table.indexes
            },
        }
    return snapshot


def _type_signature(column_type):
    if isinstance(column_type, PG_UUID):
        return ("uuid",)
    if isinstance(column_type, JSONB):
        return ("jsonb",)
    if isinstance(column_type, Numeric):
        return ("numeric", column_type.precision, column_type.scale)
    if isinstance(column_type, String):
        return ("string", column_type.length)
    if isinstance(column_type, DateTime):
        return ("datetime", column_type.timezone)
    if isinstance(column_type, Date):
        return ("date",)
    if isinstance(column_type, Boolean):
        return ("boolean",)
    if isinstance(column_type, Integer):
        return ("integer",)
    raise AssertionError(f"unsupported schema type: {column_type!r}")


def _check_signature(sql: str, columns) -> tuple[frozenset[str], tuple[str, ...]]:
    lowered = sql.lower()
    names = [
        column["name"] if isinstance(column, dict) else column.name
        for column in columns
    ]
    column_names = frozenset(
        name
        for name in names
        if re.search(
            rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])",
            lowered,
        )
    )
    string_literals = [f"string:{value}" for value in re.findall(r"'([^']*)'", sql)]
    numeric_literals = [
        f"number:{value}"
        for value in re.findall(r"(?<![a-z0-9_])\d+(?:\.\d+)?", lowered)
    ]
    literals = tuple(sorted(string_literals + numeric_literals))
    return column_names, literals


def _assert_schema_snapshot(expected, actual) -> None:
    assert actual == expected
