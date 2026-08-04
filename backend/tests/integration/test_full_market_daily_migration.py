from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic/versions/20260730_0034_full_market_daily_batch.py"


def test_full_market_daily_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260804_0044"]
    assert scripts.get_revision("20260730_0034").down_revision == "20260730_0033"


def test_full_market_daily_migration_stores_plan_and_progress() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "plan_snapshot" in source
    assert "requested_count" in source
    assert "pending_retry_count" in source
    assert "daily_batch_progress_nonnegative" in source
