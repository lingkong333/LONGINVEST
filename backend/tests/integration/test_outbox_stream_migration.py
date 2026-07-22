from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from long_invest.platform.outbox.models import EventOutbox

BACKEND = Path(__file__).parents[2]


def test_outbox_stream_migration_is_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["20260722_0021"]
    assert scripts.get_revision("20260722_0017").down_revision == "20260722_0016"


def test_outbox_stream_sequence_is_immutable_database_identity() -> None:
    column = EventOutbox.__table__.c.stream_sequence
    assert column.identity is not None
    assert column.identity.always is True
    assert column.nullable is False
