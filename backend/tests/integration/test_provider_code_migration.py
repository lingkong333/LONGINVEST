from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).parents[2]
MIGRATION = (
    BACKEND
    / "alembic/versions/20260723_0023_normalize_provider_codes.py"
)


def test_provider_code_migration_is_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260723_0023"]
    assert scripts.get_revision("20260723_0023").down_revision == "20260723_0022"


def test_provider_code_migration_normalizes_and_guards_all_owned_tables() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    for table_name in (
        "provider_config_version",
        "provider_capability_setting",
        "provider_health_state",
        "provider_circuit_history",
        "provider_circuit_state",
        "provider_failure_sample",
    ):
        assert f'"{table_name}"' in source
    assert "SET provider_code = upper(provider_code)" in source
    assert "provider_code IN ({SUPPORTED_CODES})" in source
    assert "DISABLE" in source
    assert "ENABLE" in source
    assert "SET provider_code = lower(provider_code)" in source
