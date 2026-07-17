from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic" / "versions" / "20260716_0010_monitoring_foundations.py"
ALEMBIC_ENV = BACKEND / "alembic" / "env.py"

TABLES = (
    "watchlist",
    "watchlist_item",
    "monitor_schedule",
    "monitor_schedule_revision",
    "user_position",
    "user_position_history",
    "monitor_subscription",
    "monitor_subscription_revision",
    "schedule_occurrence",
)
IMMUTABLE_TABLES = {
    "monitor_schedule_revision",
    "user_position_history",
    "monitor_subscription_revision",
}


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_monitoring_migration_extends_the_single_main_chain() -> None:
    source = _source()

    assert 'revision: str = "20260716_0010"' in source
    assert 'down_revision: str | None = "20260716_0009"' in source
    for table_name in TABLES:
        assert f'op.create_table(\n        "{table_name}"' in source


def test_monitoring_models_are_registered_with_alembic_metadata() -> None:
    source = ALEMBIC_ENV.read_text(encoding="utf-8")

    for module_name in (
        "watchlists.models",
        "monitor_schedules.models",
        "positions.models",
        "monitoring.models",
    ):
        assert f"long_invest.modules.{module_name}" in source


def test_monitoring_migration_creates_pointer_foreign_keys_after_history() -> None:
    source = _source()

    for owner_table, history_table, foreign_key in (
        (
            "monitor_schedule",
            "monitor_schedule_revision",
            "fk_monitor_schedule_current_revision",
        ),
        (
            "user_position",
            "user_position_history",
            "fk_user_position_latest_history",
        ),
        (
            "monitor_subscription",
            "monitor_subscription_revision",
            "fk_monitor_subscription_current_revision",
        ),
    ):
        owner = source.index(f'op.create_table(\n        "{owner_table}"')
        history = source.index(f'op.create_table(\n        "{history_table}"')
        pointer = source.index(f'"{foreign_key}"')
        assert owner < history < pointer


def test_monitoring_migration_has_named_constraints_and_partial_index() -> None:
    source = _source()

    assert '"uq_monitor_subscription_open_security"' in source
    assert 'postgresql_where=sa.text("archived_at IS NULL")' in source
    assert '"uq_user_position_history_security_version"' in source
    assert '"uq_schedule_occurrence_scope"' in source
    assert 'op.f("ck_monitor_subscription_archive_consistent")' in source
    assert 'op.f("ck_monitor_subscription_revision_hysteresis_nonnegative")' in source


def test_monitoring_migration_restricts_application_role_on_immutable_tables() -> None:
    source = _source()

    assert "database_app_role" in source
    assert (
        'op.execute(f"REVOKE UPDATE, DELETE ON TABLE {table_name} FROM {role}")'
        in source
    )
    for table_name in (
        "monitor_schedule_revision",
        "user_position_history",
        "monitor_subscription_revision",
    ):
        assert f'"{table_name}"' in source.split("IMMUTABLE_TABLES =", maxsplit=1)[1]


def test_monitoring_migration_quotes_and_bounds_the_application_role() -> None:
    source = _source()

    assert 'r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"' in source
    assert "return f'\"{role}\"'" in source


def test_monitoring_migration_downgrades_in_reverse_dependency_order() -> None:
    downgrade = _source().split("def downgrade() -> None:", maxsplit=1)[1]

    subscription_pointer = (
        'op.drop_constraint(\n        "fk_monitor_subscription_current_revision"'
    )
    position_pointer = 'op.drop_constraint(\n        "fk_user_position_latest_history"'
    schedule_pointer = (
        'op.drop_constraint(\n        "fk_monitor_schedule_current_revision"'
    )
    pointer_drops = [
        downgrade.index(subscription_pointer),
        downgrade.index(position_pointer),
        downgrade.index(schedule_pointer),
    ]
    table_drops = [
        downgrade.index(f'op.drop_table("{table_name}")')
        for table_name in reversed(TABLES)
    ]
    assert max(pointer_drops) < min(table_drops)
    assert table_drops == sorted(table_drops)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_monitoring_schema_and_application_role_privileges() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        async with database.session() as session:
            schema = await session.run_sync(_inspect_monitoring_schema)
            assert schema["tables"] >= set(TABLES)
            assert schema["foreign_keys"]["monitor_schedule"] >= {
                "fk_monitor_schedule_current_revision"
            }
            assert schema["foreign_keys"]["user_position"] >= {
                "fk_user_position_latest_history"
            }
            assert schema["foreign_keys"]["monitor_subscription"] >= {
                "fk_monitor_subscription_current_revision"
            }
            assert schema["indexes"]["monitor_subscription"] >= {
                "uq_monitor_subscription_open_security"
            }

            for table_name in TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    allowed = await session.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "current_user, :table_name, :privilege)"
                        ),
                        {"table_name": table_name, "privilege": privilege},
                    )
                    expected = not (
                        table_name in IMMUTABLE_TABLES
                        and privilege in {"UPDATE", "DELETE"}
                    )
                    assert allowed is expected
    finally:
        await database.dispose()


def _inspect_monitoring_schema(sync_session):
    inspector = inspect(sync_session.connection())
    return {
        "tables": set(inspector.get_table_names()),
        "foreign_keys": {
            table_name: {
                item["name"]
                for item in inspector.get_foreign_keys(table_name)
                if item.get("name")
            }
            for table_name in (
                "monitor_schedule",
                "user_position",
                "monitor_subscription",
            )
        },
        "indexes": {
            "monitor_subscription": {
                item["name"]
                for item in inspector.get_indexes("monitor_subscription")
                if item.get("name")
            }
        },
    }
