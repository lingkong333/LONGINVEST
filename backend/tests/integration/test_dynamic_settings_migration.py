from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from long_invest.modules.settings.models import (
    SecretValue,
    SystemSetting,
    SystemSettingHistory,
)
from long_invest.platform.database.base import Base

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic" / "versions" / "20260722_0015_dynamic_settings.py"


def test_dynamic_settings_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260723_0022"]
    assert scripts.get_revision("20260722_0015").down_revision == "20260722_0014"


def test_dynamic_setting_models_are_registered() -> None:
    for model in (SystemSetting, SystemSettingHistory, SecretValue):
        assert Base.metadata.tables[model.__tablename__] is model.__table__


def test_migration_seeds_allowlist_and_keeps_history_append_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "notification.policy.global" in source
    assert "notification.channel.wecom" in source
    assert "notification.channel.email" in source
    assert "GRANT SELECT, INSERT ON system_setting_history" in source
    assert "UPDATE ON system_setting_history" not in source
    assert "DELETE ON system_setting_history" not in source
