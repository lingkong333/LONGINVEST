from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic/versions/20260727_0026_sina_history_routes.py"
)


def test_sina_history_route_migration_extends_main_chain() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260727_0026"' in source
    assert 'down_revision: str | None = "20260723_0025"' in source
    assert "HISTORICAL_DAILY_UNADJUSTED" in source
    assert "HISTORICAL_DAILY_QFQ" in source
    assert "0.333333" in source
    assert "300.0" in source
    assert "manual history fallback" in source
