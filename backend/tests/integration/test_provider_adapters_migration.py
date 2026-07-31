from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic/versions/20260730_0033_provider_adapters.py"


def test_provider_adapters_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260731_0037"]
    assert scripts.get_revision("20260730_0033").down_revision == "20260729_0032"


def test_provider_adapters_migration_registers_disabled_probe_gated_sources() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "TUSHARE_SDK" in source
    assert "BAOSTOCK_SDK" in source
    assert "secret://provider.tushare.token" in source
    assert "'UNKNOWN'" in source
    assert "false" in source
    assert "provider_budget_policy" in source
