from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from long_invest.modules.notifications.models import (
    NotificationChannelCircuit,
    NotificationTemplateActivation,
    NotificationTemplateVersion,
)
from long_invest.platform.database.base import Base

BACKEND = Path(__file__).parents[2]
MIGRATION = (
    BACKEND / "alembic" / "versions" / "20260722_0019_notification_policy_runtime.py"
)


def test_notification_policy_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260723_0022"]
    assert scripts.get_revision("20260722_0019").down_revision == "20260722_0018"


def test_notification_policy_models_are_registered() -> None:
    for model in (
        NotificationTemplateVersion,
        NotificationTemplateActivation,
        NotificationChannelCircuit,
    ):
        assert Base.metadata.tables[model.__tablename__] is model.__table__


def test_notification_policy_migration_contains_runtime_state() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "notification_channels" in source
    assert "notification_template_version" in source
    assert "notification_template_activation" in source
    assert "notification_channel_circuit" in source
    assert "circuit_deferred_until" in source
