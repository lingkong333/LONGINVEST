from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic/versions/20260722_0021_system_schedule_occurrences.py"


def test_system_schedule_migration_is_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260723_0023"]
    assert scripts.get_revision("20260722_0021").down_revision == "20260722_0020"


def test_system_schedule_migration_defines_scope_and_runtime_grant() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "definition_key" in source
    assert "scheduled_trade_date" in source
    assert "calendar_version_id" in source
    assert "ck_schedule_occurrence_definition_scope_valid" in source
    assert "uq_schedule_occurrence_system_scope" in source
    assert "scheduler_runtime_state" in source
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in source
