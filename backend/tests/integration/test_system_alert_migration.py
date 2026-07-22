from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from long_invest.modules.alerts.models import (
    SystemAlert,
    SystemAlertAction,
    SystemAlertOccurrence,
)
from long_invest.platform.database.base import Base

BACKEND = Path(__file__).parents[2]


def test_system_alert_migration_is_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["20260722_0017"]
    assert scripts.get_revision("20260722_0016").down_revision == "20260722_0015"


def test_system_alert_models_are_registered() -> None:
    for model in (SystemAlert, SystemAlertOccurrence, SystemAlertAction):
        assert Base.metadata.tables[model.__tablename__] is model.__table__
