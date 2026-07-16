from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.base import Base
from long_invest.platform.database.engine import Database

EXPECTED_TABLES = {
    "data_quality_issue",
    "quote_cycle",
    "quote_cycle_item",
    "daily_data_batch",
    "daily_bar_stage",
    "daily_bar_unadjusted",
    "daily_bar_revision",
    "daily_batch_missing_item",
}
EXPECTED_CONSTRAINTS = {
    "data_quality_issue": {
        "ck_data_quality_issue_occurrence_count_positive",
        "ck_data_quality_issue_status_valid",
        "ck_data_quality_issue_severity_valid",
        "ck_data_quality_issue_evidence_non_empty_object",
        "uq_data_quality_issue_dedupe_key",
    },
    "quote_cycle": {
        "ck_quote_cycle_status_valid",
        "uq_quote_cycle_idempotency",
        "uq_quote_cycle_schedule_occurrence",
    },
    "quote_cycle_item": {
        "ck_quote_cycle_item_status_valid",
        "uq_quote_cycle_item_symbol",
        "fk_quote_cycle_item_cycle_id_quote_cycle",
    },
    "daily_data_batch": {
        "ck_daily_data_batch_daily_batch_status_valid",
        "uq_daily_batch_idempotency",
        "fk_daily_data_batch_parent_batch_id_daily_data_batch",
    },
    "daily_bar_stage": {
        "ck_daily_bar_stage_daily_stage_status_valid",
        "uq_daily_stage_symbol",
        "fk_daily_bar_stage_batch_id_daily_data_batch",
    },
    "daily_bar_unadjusted": {
        "ck_daily_bar_unadjusted_daily_bar_prices_positive",
        "ck_daily_bar_unadjusted_daily_bar_ohlc_valid",
        "ck_daily_bar_unadjusted_daily_bar_quantities_nonnegative",
    },
    "daily_bar_revision": {
        "uq_daily_bar_revision_no",
        "fk_daily_revision_bar",
    },
    "daily_batch_missing_item": {
        "ck_daily_batch_missing_item_daily_missing_reason_valid",
        "uq_daily_missing_symbol",
        "fk_daily_batch_missing_item_batch_id_daily_data_batch",
    },
}
EXPECTED_INDEXES = {
    "data_quality_issue": {
        "ix_data_quality_issue_status_last_seen",
        "ix_data_quality_issue_symbol_status",
    },
    "quote_cycle": {"ix_quote_cycle_status_deadline"},
    "quote_cycle_item": {"ix_quote_cycle_item_cycle_status"},
    "daily_data_batch": {"ix_daily_batch_date_status", "uq_daily_batch_auto_scope"},
    "daily_bar_stage": {"ix_daily_stage_batch_status", "ix_daily_stage_expires_at"},
    "daily_bar_unadjusted": {"ix_daily_bar_symbol_date"},
    "daily_bar_revision": {"ix_daily_revision_symbol_date"},
    "daily_batch_missing_item": {"ix_daily_missing_batch_explained"},
}


def test_market_data_models_are_registered_with_shared_metadata() -> None:
    from long_invest.modules.daily_data import models as _daily_models  # noqa: F401
    from long_invest.modules.market_data import models as _quality_models  # noqa: F401
    from long_invest.modules.quotes import models as _quote_models  # noqa: F401

    assert set(Base.metadata.tables) >= EXPECTED_TABLES


def test_market_data_migration_is_the_single_successor_to_0007() -> None:
    migration = (
        Path(__file__).parents[2]
        / "alembic"
        / "versions"
        / "20260715_0008_market_data_collection.py"
    )

    source = migration.read_text(encoding="utf-8")

    assert 'revision: str = "20260715_0008"' in source
    assert 'down_revision: str | None = "20260715_0007"' in source
    assert "PARTITION_YEARS = (2025, 2026, 2027)" in source
    assert "CREATE TABLE daily_bar_unadjusted_{year}" in source
    assert "known_corporate_action_symbols JSONB NOT NULL" in source
    drop_at = source.index(
        "DROP TRIGGER security_universe_snapshot_item_append_only"
    )
    update_at = source.index("UPDATE security_universe_snapshot_item AS item")
    recreate_at = source.index(
        "CREATE TRIGGER security_universe_snapshot_item_append_only"
    )
    assert drop_at < update_at < recreate_at


@pytest.mark.anyio
async def test_market_data_schema_and_year_partition_exist() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        async with database.transaction() as session:
            schema = await session.run_sync(_inspect_market_data_schema)
            tables = schema["tables"]
            assert tables >= EXPECTED_TABLES
            for table_name, expected in EXPECTED_CONSTRAINTS.items():
                assert schema["constraints"][table_name] >= expected
            for table_name, expected in EXPECTED_INDEXES.items():
                assert schema["indexes"][table_name] >= expected

            job_columns = schema["job_columns"]
            assert {"soft_timeout_seconds", "hard_timeout_seconds"} <= job_columns
            assert (
                "known_corporate_action_symbols"
                in schema["daily_batch_columns"]
            )
            assert "security_id" in schema["security_snapshot_columns"]
            assert "listed_on" in schema["security_snapshot_columns"]
            assert "delisted_on" in schema["security_snapshot_columns"]
            assert (
                "fk_security_universe_snapshot_item_security_id_security"
                in schema["security_snapshot_foreign_keys"]
            )

            partitions = set(
                (
                    await session.scalars(
                        text(
                            """
                            SELECT inhrelid::regclass::text
                            FROM pg_inherits
                            WHERE inhparent = 'daily_bar_unadjusted'::regclass
                            """
                        )
                    )
                ).all()
            )
            assert partitions == {
                "daily_bar_unadjusted_2025",
                "daily_bar_unadjusted_2026",
                "daily_bar_unadjusted_2027",
            }

            for table_name in EXPECTED_TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert await session.scalar(
                        text("SELECT has_table_privilege(:table, :privilege)"),
                        {"table": table_name, "privilege": privilege},
                    )

            for year in (2025, 2026, 2027):
                row_security_id = uuid4()
                trade_date = date(year, 7, 16)
                await session.execute(
                    text(
                        """
                    INSERT INTO daily_bar_unadjusted (
                        security_id, trade_date, symbol, open, high, low, close,
                        previous_close, volume, amount, source, data_version
                    ) VALUES (
                        :security_id, :trade_date, '600000.SH', 10, 11, 9, 10.5,
                        9.8, 3000000000, 123456.78, 'eastmoney', 1
                    )
                        """
                    ),
                    {"security_id": row_security_id, "trade_date": trade_date},
                )
                partition = await session.scalar(
                    text(
                        """
                    SELECT tableoid::regclass::text
                    FROM daily_bar_unadjusted
                    WHERE security_id = :security_id AND trade_date = :trade_date
                        """
                    ),
                    {"security_id": row_security_id, "trade_date": trade_date},
                )
                assert partition == f"daily_bar_unadjusted_{year}"
            raise _RollbackMigrationFixture
    except _RollbackMigrationFixture:
        pass
    finally:
        await database.dispose()


class _RollbackMigrationFixture(Exception):
    pass


def _inspect_market_data_schema(sync_session):
    inspector = inspect(sync_session.connection())
    constraints: dict[str, set[str]] = {}
    indexes: dict[str, set[str]] = {}
    for table_name in EXPECTED_TABLES:
        constraints[table_name] = {
            item["name"]
            for reader in (
                inspector.get_check_constraints,
                inspector.get_unique_constraints,
                inspector.get_foreign_keys,
            )
            for item in reader(table_name)
            if item.get("name")
        }
        indexes[table_name] = {
            item["name"]
            for item in inspector.get_indexes(table_name)
            if item.get("name")
        }
    return {
        "tables": set(inspector.get_table_names()),
        "constraints": constraints,
        "indexes": indexes,
        "job_columns": {
            column["name"] for column in inspector.get_columns("job")
        },
        "daily_batch_columns": {
            column["name"]
            for column in inspector.get_columns("daily_data_batch")
        },
        "security_snapshot_columns": {
            column["name"]
            for column in inspector.get_columns("security_universe_snapshot_item")
        },
        "security_snapshot_foreign_keys": {
            item["name"]
            for item in inspector.get_foreign_keys(
                "security_universe_snapshot_item"
            )
            if item.get("name")
        },
    }
