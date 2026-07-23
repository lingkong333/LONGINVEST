from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).parents[2]
MIGRATION = (
    BACKEND
    / "alembic/versions/20260723_0022_strategy_draft_facts.py"
)


def test_strategy_draft_facts_migration_is_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260723_0022"]
    assert scripts.get_revision("20260723_0022").down_revision == "20260722_0021"


def test_strategy_draft_facts_migration_is_narrow_and_reversible() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert '("strategy_draft", "strategy_draft_revision")' in source
    assert '("strategy_draft_revision", "strategy_draft")' in source
    assert '"metadata"' in source
    assert '"parameter_schema"' in source
    assert "postgresql.JSONB" in source
    assert "nullable=False" in source
    assert "server_default=EMPTY_JSON" in source
    assert 'op.drop_column(table_name, "parameter_schema")' in source
    assert 'op.drop_column(table_name, "metadata")' in source
