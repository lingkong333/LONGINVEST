import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from long_invest.modules.signals.models import (
    SignalEvaluation,
    SignalEvent,
    SignalState,
)
from long_invest.modules.targets.models import SubscriptionTargetBinding, TargetRevision
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.base import Base
from long_invest.platform.database.engine import Database

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
            assert schema["tables"] >= set(TABLES)
            assert schema["types"]["target_revision"]["low_strong"] == "NUMERIC(20, 2)"
            assert schema["types"]["signal_state"]["last_price"] == "NUMERIC(20, 6)"
            assert schema["nullable"]["signal_event"]["notification_eligible"] is False
            assert schema["constraints"]["target_revision"] >= {
                "ck_target_revision_values_ordered",
                "ck_target_revision_source_revision_consistent",
                "uq_target_revision_idempotency",
            }
            assert schema["constraints"]["signal_evaluation"] >= {
                "ck_signal_evaluation_non_skipped_inputs_complete",
                "ck_signal_evaluation_target_values_valid",
                "uq_signal_evaluation_idempotency",
            }
            assert schema["indexes"]["signal_event"] >= {
                "ix_signal_event_subscription_created",
                "ix_signal_event_notification_eligible",
            }
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
    return {
        "tables": set(inspector.get_table_names()),
        "types": {
            table: {
                column["name"]: str(column["type"])
                for column in inspector.get_columns(table)
            }
            for table in TABLES
        },
        "nullable": {
            table: {
                column["name"]: column["nullable"]
                for column in inspector.get_columns(table)
            }
            for table in TABLES
        },
        "constraints": {
            table: {
                item["name"]
                for item in (
                    inspector.get_check_constraints(table)
                    + inspector.get_unique_constraints(table)
                )
                if item.get("name")
            }
            for table in TABLES
        },
        "indexes": {
            table: {
                item["name"]
                for item in inspector.get_indexes(table)
                if item.get("name")
            }
            for table in TABLES
        },
    }
