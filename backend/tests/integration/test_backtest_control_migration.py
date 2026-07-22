from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from long_invest.modules.backtests.models import BacktestControlCommand
from long_invest.platform.database.base import Base

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic" / "versions" / "20260722_0014_backtest_controls.py"


def test_backtest_control_migration_is_the_single_head() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20260722_0014"]
    assert scripts.get_revision("20260722_0014").down_revision == "20260722_0013"


def test_backtest_control_model_is_registered_with_alembic_metadata() -> None:
    assert (
        Base.metadata.tables[BacktestControlCommand.__tablename__]
        is BacktestControlCommand.__table__
    )


def test_backtest_control_migration_enforces_generation_and_idempotency() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "execution_generation" in source
    assert "rerun_from_task_id" in source
    assert "backtest_control_command" in source
    assert "uq_backtest_control_command_idempotency_key" in source
    assert "GRANT SELECT, INSERT" in source
