from functools import lru_cache

from alembic.config import Config
from alembic.script import ScriptDirectory


@lru_cache
def expected_database_revisions() -> frozenset[str]:
    config = Config("alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    return frozenset(scripts.get_heads())
