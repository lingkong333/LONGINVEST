from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).parents[2]
MIGRATION = (
    BACKEND
    / "alembic"
    / "versions"
    / "20260723_0024_historical_daily_partitions.py"
)


def test_historical_partition_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))

    heads = ScriptDirectory.from_config(config).get_heads()

    assert heads == ["20260729_0030"]


def test_historical_partition_migration_covers_the_a_share_history_window() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260723_0024"' in source
    assert 'down_revision: str | None = "20260723_0023"' in source
    assert "tuple(range(1990, 2025))" in source
    assert "CREATE TABLE daily_bar_unadjusted_{year}" in source
    assert "DROP TABLE daily_bar_unadjusted_{year}" in source
