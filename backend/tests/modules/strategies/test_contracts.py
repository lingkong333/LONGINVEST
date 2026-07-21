# ruff: noqa: E501
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.strategies.contracts import (
    StrategyForecastErrorCode,
    StrategyForecastRequest,
    StrategyForecastResult,
    StrategyReadiness,
    StrategyReadinessStatus,
    TrainingDataSnapshot,
)
from long_invest.modules.targets.contracts import TargetValues


def test_strategy_forecast_contract_freezes_training_only_input() -> None:
    snapshot = TrainingDataSnapshot(
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
        strategy_version_id=uuid4(),
        source_code_hash="a" * 64,
        parameter_snapshot={"nested": {"value": 1}},
        parameter_hash="b" * 64,
        training_data=TrainingDataSnapshot(
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
