from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic/versions/20260729_0032_multi_provider_routing.py"


def test_multi_provider_routing_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260731_0038"]
    assert scripts.get_revision("20260729_0032").down_revision == "20260729_0031"


def test_multi_provider_routing_migration_covers_routing_and_provenance() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "provider_capability_registration" in source
    assert "provider_route_policy_version" in source
    assert "'UNKNOWN','PASSED','FAILED'" in source
    assert "TUSHARE" in source
    assert "BAOSTOCK" in source
    assert "source_identity" in source
    assert "daily_bar_unadjusted" in source
    assert "quote_cycle_item" in source
    assert "security_master_version" in source
