from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic/versions/20260728_0028_sina_history_concurrency.py"
)


def test_sina_history_concurrency_migration_extends_main_chain() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260728_0028"' in source
    assert 'down_revision: str | None = "20260728_0027"' in source
    assert "HISTORICAL_DAILY_%" in source
    assert "THEN 4 ELSE current.concurrency" in source
    assert "four-concurrency history backfill" in source
