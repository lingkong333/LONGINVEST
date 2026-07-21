# ruff: noqa: E501
from long_invest.modules.strategies.models import (
    Strategy,
    StrategyDraft,
    StrategyDraftRevision,
    StrategyRun,
    StrategyValidationRun,
    StrategyVersion,
)


def test_strategy_module_owns_all_lifecycle_records() -> None:
    assert {
        Strategy.__tablename__,
        StrategyDraft.__tablename__,
        StrategyDraftRevision.__tablename__,
        StrategyVersion.__tablename__,
        StrategyValidationRun.__tablename__,
        StrategyRun.__tablename__,
    } == {
        "strategy",
        "strategy_draft",
        "strategy_draft_revision",
        "strategy_version",
        "strategy_validation_run",
        "strategy_run",
    }
    assert {
        "source_code",
        "parameter_schema",
        "environment_version",
        "image_digest",
        "git_commit",
        "validation_run_id",
        "published_at",
    } <= set(StrategyVersion.__table__.c.keys())
