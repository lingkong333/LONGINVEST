from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

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
        "runner_image_digest",
        "git_commit",
        "validation_run_id",
        "published_at",
    } <= set(StrategyVersion.__table__.c.keys())


def _constraint_names(model: type) -> set[str | None]:
    return {constraint.name for constraint in model.__table__.constraints}


def _foreign_key_targets(model: type) -> set[str]:
    return {
        element.target_fullname
        for constraint in model.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }


def test_strategy_models_enforce_lifecycle_and_immutable_version_invariants() -> None:
    assert {
        "metadata",
        "runner_image_digest",
        "source_code_hash",
        "validation_run_id",
    } <= set(StrategyVersion.__table__.c.keys())
    assert {
        "ck_strategy_status_valid",
        "ck_strategy_draft_version_positive",
        "ck_strategy_draft_revision_revision_positive",
        "ck_strategy_version_status_valid",
        "ck_strategy_version_source_code_hash_sha256",
        "ck_strategy_version_runner_image_digest_sha256",
        "ck_strategy_version_publication_consistent",
        "ck_strategy_validation_run_status_valid",
        "ck_strategy_run_status_valid",
    } <= (
        _constraint_names(Strategy)
        | _constraint_names(StrategyDraft)
        | _constraint_names(StrategyDraftRevision)
        | _constraint_names(StrategyVersion)
        | _constraint_names(StrategyValidationRun)
        | _constraint_names(StrategyRun)
    )
    assert any(
        isinstance(item, UniqueConstraint)
        and tuple(column.name for column in item.columns)
        == ("strategy_id", "version_no")
        for item in StrategyVersion.__table__.constraints
    )
    assert "strategy.id" in _foreign_key_targets(StrategyVersion)
    assert "strategy_validation_run.id" in _foreign_key_targets(StrategyVersion)
    assert "strategy_version.id" in _foreign_key_targets(StrategyRun)
    source_hash_constraint = next(
        item
        for item in StrategyVersion.__table__.constraints
        if item.name == "ck_strategy_version_source_code_hash_sha256"
    )
    assert "source_code_hash ~ '^[0-9a-f]{64}$'" in str(source_hash_constraint.sqltext)
