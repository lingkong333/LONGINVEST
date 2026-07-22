from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from long_invest.modules.backtests.models import BacktestAdjustmentSnapshot
from long_invest.modules.market_data.models import (
    CorporateActionFact,
    CorporateActionFetchBatch,
)
from long_invest.platform.database.base import Base

BACKEND = Path(__file__).parents[2]
MIGRATION = BACKEND / "alembic" / "versions" / "20260722_0013_corporate_actions.py"


def test_corporate_action_migration_precedes_backtest_controls() -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["20260722_0016"]
    assert scripts.get_revision("20260722_0014").down_revision == "20260722_0013"


def test_corporate_action_models_are_registered_with_alembic_metadata() -> None:
    for model in (
        CorporateActionFetchBatch,
        CorporateActionFact,
        BacktestAdjustmentSnapshot,
    ):
        assert Base.metadata.tables[model.__tablename__] is model.__table__


def test_corporate_action_migration_enforces_evidence_and_least_privilege() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "20260721_0012"' in source
    assert "corporate_action_fetch_batch" in source
    assert "corporate_action_fact" in source
    assert "backtest_adjustment_snapshot" in source
    assert "CORPORATE_ACTIONS" in source
    assert "REVOKE UPDATE, DELETE" in source
    assert "reject_corporate_action_fact_mutation" in source
