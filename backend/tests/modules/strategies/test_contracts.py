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
        rows=({"trade_date": "2025-12-31", "close": "10.00"},),
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
