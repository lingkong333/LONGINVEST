from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_bulk_job_timeout_migration_is_the_single_head() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260722_0018"]
    assert scripts.get_revision("20260722_0018").down_revision == "20260722_0017"
