from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "20260723_0025_security_master_fallback.py"
)


def test_security_master_fallback_migration_extends_routes_append_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision: str | None = "20260723_0024"' in source
    assert "'SECURITY_MASTER', TRUE" in source
    assert "'SECURITY_MASTER', 1" in source
    assert "enable security master fallback" in source
    assert "add low frequency security master fallback" in source
