import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.strategies.contracts import (
    StrategyForecastErrorCode,
    StrategyForecastRequest,
    StrategyForecastResult,
    StrategyLifecycleErrorCode,
    StrategyLifecycleStatus,
    StrategyReadiness,
    StrategyReadinessStatus,
    StrategyRunStatus,
    StrategyRunView,
    StrategyValidationRunView,
    StrategyVersionView,
    TrainingDataSnapshot,
    ValidationRunStatus,
)
from long_invest.modules.targets.contracts import TargetValues

FROZEN_AT = datetime(2026, 7, 21, tzinfo=UTC)
SOURCE = "def calculate_targets(history, params, context): return {}"


def _strategy_fields() -> dict[str, object]:
    return {
        "strategy_id": uuid4(),
        "security_name": "浦发银行",
        "draft_id": None,
        "draft_version": None,
        "source_code": SOURCE,
        "metadata": {},
        "parameter_schema": {},
        "environment_version": "runner-1",
        "runner_image_digest": "sha256:" + "d" * 64,
    }


def _data_provenance() -> dict[str, object]:
    return {
        "fetched_at": FROZEN_AT,
        "source": "EASTMONEY",
        "price_basis": "QFQ_AS_OF",
    }


def test_strategy_forecast_contract_freezes_training_only_input() -> None:
    snapshot = TrainingDataSnapshot(
        **_data_provenance(),
        security_id=uuid4(),
        symbol="600000.SH",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        data_version=1,
        content_hash="a" * 64,
        rows=(
            {
                "trade_date": date(2025, 12, 31),
                "open": "10",
                "high": "10",
                "low": "10",
                "close": "10",
            },
        ),
    )
    request = StrategyForecastRequest(
        **_strategy_fields(),
        strategy_version_id=uuid4(),
        source_code_hash="b" * 64,
        parameter_snapshot={"window": 20},
        parameter_hash="c" * 64,
        training_data=snapshot,
        requested_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert request.training_data.end_date == date(2025, 12, 31)
    with pytest.raises(ValidationError):
        request.training_data = snapshot


def test_strategy_forecast_result_requires_valid_target_values() -> None:
    result = StrategyForecastResult(
        values=TargetValues(
            low_strong=Decimal("8"),
            low_watch=Decimal("9"),
            high_watch=Decimal("11"),
            high_strong=Decimal("12"),
        ),
        diagnostics={"sample_count": 250},
    )
    assert result.diagnostics["sample_count"] == 250


def test_training_data_rejects_rows_outside_the_training_window() -> None:
    with pytest.raises(ValidationError):
        TrainingDataSnapshot(
            **_data_provenance(),
            security_id=uuid4(),
            symbol="600000.SH",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 1),
            data_version=1,
            content_hash="a" * 64,
            rows=(
                {
                    "trade_date": date(2025, 1, 2),
                    "open": "9",
                    "high": "11",
                    "low": "8",
                    "close": "10",
                },
            ),
        )


def test_forecast_request_deeply_freezes_nested_parameters() -> None:
    request = StrategyForecastRequest(
        **_strategy_fields(),
        strategy_version_id=uuid4(),
        source_code_hash="a" * 64,
        parameter_snapshot={"nested": {"value": 1}},
        parameter_hash="b" * 64,
        training_data=TrainingDataSnapshot(
            **_data_provenance(),
            security_id=uuid4(),
            symbol="600000.SH",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 1),
            data_version=1,
            content_hash="c" * 64,
            rows=(
                {
                    "trade_date": date(2025, 1, 1),
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                },
            ),
        ),
        requested_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    with pytest.raises(TypeError):
        request.parameter_snapshot["nested"]["value"] = 2


def test_strategy_readiness_uses_stable_status_and_error_codes() -> None:
    readiness = StrategyReadiness(
        strategy_version_id=uuid4(),
        status=StrategyReadinessStatus.READY,
        checked_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    assert readiness.status is StrategyReadinessStatus.READY
    assert StrategyForecastErrorCode.STRATEGY_FORECAST_TIMEOUT.value == (
        "STRATEGY_FORECAST_TIMEOUT"
    )


def test_strategy_lifecycle_statuses_and_error_codes_are_complete() -> None:
    assert {status.value for status in StrategyLifecycleStatus} == {
        "DRAFT",
        "VALIDATING",
        "VALIDATED",
        "PUBLISHING",
        "PUBLISHED",
        "PUBLISH_FAILED",
        "ARCHIVED",
    }
    assert {code.value for code in StrategyLifecycleErrorCode} == {
        "STRATEGY_VERSION_CONFLICT",
        "STRATEGY_NOT_READY",
        "STRATEGY_VALIDATION_REQUIRED",
        "STRATEGY_VALIDATION_STALE",
        "STRATEGY_PUBLISH_IN_PROGRESS",
        "STRATEGY_PUBLISH_FAILED",
        "STRATEGY_VERSION_IMMUTABLE",
        "STRATEGY_ARCHIVED",
    }


def test_validation_and_strategy_run_statuses_match_persistence_constraints() -> None:
    assert {status.value for status in ValidationRunStatus} == {
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
    }
    assert {status.value for status in StrategyRunStatus} == {
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELED",
    }
    validation = StrategyValidationRunView(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_version_id=None,
        draft_version=3,
        source_code_hash="a" * 64,
        evidence_snapshot={"static_analysis": "passed"},
        status=ValidationRunStatus.SUCCEEDED,
        error_code=None,
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
        completed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    run = StrategyRunView(
        id=uuid4(),
        strategy_version_id=uuid4(),
        status=StrategyRunStatus.CANCELED,
    )
    assert validation.status is ValidationRunStatus.SUCCEEDED
    assert run.status is StrategyRunStatus.CANCELED
    with pytest.raises(ValidationError):
        StrategyValidationRunView(
            **(validation.model_dump() | {"status": StrategyLifecycleStatus.PUBLISHED})
        )


def test_validation_run_freezes_exact_validated_draft_and_evidence() -> None:
    validation = StrategyValidationRunView(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_version_id=None,
        draft_version=2,
        source_code_hash="b" * 64,
        evidence_snapshot={"checks": ["static", "sandbox"]},
        status=ValidationRunStatus.SUCCEEDED,
        error_code=None,
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
        completed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert validation.model_dump(mode="json")["evidence_snapshot"] == {
        "checks": ["static", "sandbox"]
    }
    with pytest.raises(TypeError):
        validation.evidence_snapshot["checks"] = []  # type: ignore[index]


def test_validation_run_requires_consistent_completion_and_failure_metadata() -> None:
    values = {
        "id": uuid4(),
        "strategy_id": uuid4(),
        "strategy_version_id": None,
        "draft_version": 1,
        "source_code_hash": "c" * 64,
        "evidence_snapshot": {},
        "status": ValidationRunStatus.RUNNING,
        "error_code": None,
        "created_at": datetime(2026, 7, 21, tzinfo=UTC),
        "completed_at": None,
    }
    StrategyValidationRunView(**values)
    with pytest.raises(ValidationError):
        StrategyValidationRunView(
            **(values | {"status": ValidationRunStatus.SUCCEEDED})
        )
    with pytest.raises(ValidationError):
        StrategyValidationRunView(
            **(
                values
                | {
                    "status": ValidationRunStatus.FAILED,
                    "completed_at": datetime(2026, 7, 21, tzinfo=UTC),
                }
            )
        )


def test_strategy_nested_frozen_values_dump_as_json() -> None:
    request = StrategyForecastRequest(
        **_strategy_fields(),
        strategy_version_id=uuid4(),
        source_code_hash="a" * 64,
        parameter_snapshot=MappingProxyType(
            {"nested": MappingProxyType({"values": (1, (2, 3))})}
        ),
        parameter_hash="b" * 64,
        training_data=TrainingDataSnapshot(
            **_data_provenance(),
            security_id=uuid4(),
            symbol="600000.SH",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 1),
            data_version=1,
            content_hash="c" * 64,
            rows=(
                {
                    "trade_date": date(2025, 1, 1),
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "labels": ("training",),
                },
            ),
        ),
        requested_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    result = StrategyForecastResult(
        values=TargetValues(
            low_strong="1", low_watch="2", high_watch="3", high_strong="4"
        ),
        diagnostics={"nested": {"values": (1, (2, 3))}},
    )

    request_dump = request.model_dump(mode="json")
    result_dump = result.model_dump(mode="json")
    json.dumps(request_dump)
    json.dumps(result_dump)
    assert request_dump["parameter_snapshot"]["nested"]["values"][0] == 1
    assert set(request_dump["parameter_snapshot"]["nested"]["values"][1]) == {2, 3}
    assert request_dump["training_data"]["rows"][0]["labels"] == ["training"]
    assert set(result_dump["diagnostics"]["nested"]["values"][1]) == {2, 3}


@pytest.mark.parametrize("value", [{1, 2}, frozenset({1, 2}), object()])
def test_strategy_json_snapshots_reject_unsupported_values(value: object) -> None:
    with pytest.raises(ValidationError):
        StrategyForecastResult(
            values=TargetValues(
                low_strong="1", low_watch="2", high_watch="3", high_strong="4"
            ),
            diagnostics={"value": value},
        )


def test_strategy_version_view_is_a_complete_immutable_release_snapshot() -> None:
    validation_run_id = uuid4()
    published_at = datetime(2026, 7, 21, 9, tzinfo=UTC)
    version = StrategyVersionView(
        id=uuid4(),
        strategy_id=uuid4(),
        version_no=1,
        source_code="def calculate_targets(history, params, context): ...",
        metadata={"name": "value strategy", "tags": ("long", "hold")},
        parameter_schema={"type": "object", "properties": {}},
        environment_version="python-3.12-pandas-2",
        runner_image_digest="sha256:" + "d" * 64,
        source_code_hash="a" * 64,
        git_commit="b" * 40,
        validation_run_id=validation_run_id,
        status=StrategyLifecycleStatus.PUBLISHED,
        published_at=published_at,
        created_at=published_at,
    )

    dumped = version.model_dump(mode="json")
    json.dumps(dumped)
    assert dumped["metadata"]["tags"] == ["long", "hold"]
    assert dumped["parameter_schema"] == {"type": "object", "properties": {}}
    assert version.validation_run_id == validation_run_id
    with pytest.raises(ValidationError):
        version.source_code = "changed"


@pytest.mark.parametrize(
    "status",
    [StrategyLifecycleStatus.PUBLISHING, StrategyLifecycleStatus.PUBLISH_FAILED],
)
def test_unpublished_strategy_version_allows_pending_publication_fields(
    status: StrategyLifecycleStatus,
) -> None:
    version = StrategyVersionView(
        id=uuid4(),
        strategy_id=uuid4(),
        version_no=1,
        source_code="def calculate_targets(history, params, context): ...",
        metadata={},
        parameter_schema={"type": "object"},
        environment_version="runner-1",
        runner_image_digest="sha256:" + "d" * 64,
        source_code_hash="a" * 64,
        git_commit=None,
        validation_run_id=None,
        status=status,
        published_at=None,
        created_at=datetime(2026, 7, 21, 9, tzinfo=UTC),
    )
    assert version.published_at is None


@pytest.mark.parametrize("missing", ["git_commit", "validation_run_id", "published_at"])
def test_published_strategy_version_requires_complete_publication_fields(
    missing: str,
) -> None:
    values = {
        "id": uuid4(),
        "strategy_id": uuid4(),
        "version_no": 1,
        "source_code": "def calculate_targets(history, params, context): ...",
        "metadata": {},
        "parameter_schema": {"type": "object"},
        "environment_version": "runner-1",
        "runner_image_digest": "sha256:" + "d" * 64,
        "source_code_hash": "a" * 64,
        "git_commit": "b" * 40,
        "validation_run_id": uuid4(),
        "status": StrategyLifecycleStatus.PUBLISHED,
        "published_at": datetime(2026, 7, 21, 10, tzinfo=UTC),
        "created_at": datetime(2026, 7, 21, 9, tzinfo=UTC),
    }
    values[missing] = None
    with pytest.raises(ValidationError):
        StrategyVersionView(**values)
