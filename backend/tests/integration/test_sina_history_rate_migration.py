from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic/versions/20260728_0027_sina_history_rate.py"
)


def test_sina_history_rate_migration_extends_main_chain() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260728_0027"' in source
    assert 'down_revision: str | None = "20260727_0026"' in source
    assert "HISTORICAL_DAILY_%" in source
    assert "THEN 2.0" in source
    assert "THEN 1 ELSE current.concurrency" in source
    assert "500ms serial history interval" in source
