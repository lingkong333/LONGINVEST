from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from long_invest.modules.strategies.contracts import (
    StrategyForecastErrorCode,
    StrategyForecastRequest,
    TrainingDataSnapshot,
)
from long_invest.modules.strategies.forecast import (
    StrategyForecastFailure,
    build_runner_payload,
    normalize_runner_result,
)
from long_invest.modules.strategies.runner_execution import execute_runner_payload

SOURCE = """
import numpy as np
import pandas as pd

STRATEGY_API_VERSION = "1.0"
STRATEGY_META = {
    "name": "runner test",
    "data_requirements": {
        "adjustment": "qfq",
        "min_bars": 2,
        "max_bars": 1000,
    },
    "parameter_schema": {
        "type": "object",
        "properties": {"spread": {"type": "number", "exclusiveMinimum": 0}},
        "required": ["spread"],
        "additionalProperties": False,
    },
}

def calculate_targets(history, params, context):
    assert isinstance(history, pd.DataFrame)
    assert list(history.columns) == [
        "trade_date", "open", "high", "low", "close", "volume", "amount"
    ]
    assert history["trade_date"].is_monotonic_increasing
    close = float(history["close"].iloc[-1])
    spread = params["spread"]
    return {
        "low_strong": np.float64(close * (1 - spread * 2)),
        "low_watch": np.float64(close * (1 - spread)),
        "high_watch": np.float64(close * (1 + spread)),
        "high_strong": np.float64(close * (1 + spread * 2)),
        "diagnostics": {
            "rows": int(len(history)),
            "as_of_date": context["as_of_date"],
        },
    }
"""


def _request() -> StrategyForecastRequest:
    return StrategyForecastRequest(
        strategy_version_id=uuid4(),
        source_code_hash="a" * 64,
        parameter_snapshot={"spread": 0.1},
        parameter_hash="b" * 64,
        training_data=TrainingDataSnapshot(
            security_id=uuid4(),
            symbol="600000.SH",
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 3),
            data_version=7,
            content_hash="c" * 64,
            rows=(
                {
                    "trade_date": date(2025, 1, 2),
                    "open": "9.00",
                    "high": "10.00",
                    "low": "8.50",
                    "close": "9.50",
                    "volume": "1000",
                    "amount": "9500",
                },
                {
                    "trade_date": date(2025, 1, 3),
                    "open": "9.50",
                    "high": "10.50",
                    "low": "9.00",
                    "close": "10.00",
                    "volume": "1200",
                    "amount": "12000",
                },
            ),
        ),
        requested_at=datetime(2026, 7, 21, tzinfo=UTC),
    )


def _context(request: StrategyForecastRequest) -> dict[str, object]:
    return {
        "symbol": request.training_data.symbol,
        "exchange": "SH",
        "name": "浦发银行",
        "as_of_date": request.training_data.end_date,
        "strategy_version_id": str(request.strategy_version_id),
        "data_version": request.training_data.data_version,
        "calculation_reason": "BACKTEST",
    }


def test_runner_payload_executes_with_training_dataframe_and_normalizes_targets() -> (
    None
):
    request = _request()
    payload = build_runner_payload(
        source_code=SOURCE,
        request=request,
        context=_context(request),
        schema={
            "type": "object",
            "properties": {"spread": {"type": "number", "exclusiveMinimum": 0}},
            "required": ["spread"],
            "additionalProperties": False,
        },
    )

    raw_result = execute_runner_payload(payload)
    result = normalize_runner_result(raw_result)

    assert result.values.low_strong.as_tuple().exponent == -2
    assert str(result.values.low_strong) == "8.00"
    assert str(result.values.high_strong) == "12.00"
    assert result.diagnostics == {"rows": 2, "as_of_date": "2025-01-03"}


def test_runner_payload_rejects_parameters_that_do_not_match_schema() -> None:
    request = _request().model_copy(update={"parameter_snapshot": {"spread": 0}})

    with pytest.raises(StrategyForecastFailure) as error:
        build_runner_payload(
            source_code=SOURCE,
            request=request,
            context=_context(request),
            schema={
                "type": "object",
                "properties": {"spread": {"type": "number", "exclusiveMinimum": 0}},
                "required": ["spread"],
                "additionalProperties": False,
            },
        )

    assert error.value.code == "STRATEGY_PARAMETER_INVALID"


def test_runner_payload_rejects_any_test_data_context_field() -> None:
    request = _request()
    context = _context(request)
    context["test_start_date"] = "2026-01-01"

    with pytest.raises(StrategyForecastFailure) as error:
        build_runner_payload(
            source_code=SOURCE,
            request=request,
            context=context,
            schema={"type": "object"},
        )

    assert error.value.code is StrategyForecastErrorCode.TEST_DATA_EXPOSED_TO_STRATEGY


@pytest.mark.parametrize(
    "raw",
    [
        {"low_strong": 1, "low_watch": 2, "high_watch": 3},
        {
            "low_strong": 1,
            "low_watch": 2,
            "high_watch": 3,
            "high_strong": float("nan"),
        },
        {
            "low_strong": 2,
            "low_watch": 1,
            "high_watch": 3,
            "high_strong": 4,
        },
    ],
)
def test_runner_result_rejects_missing_nonfinite_or_unordered_targets(
    raw: dict[str, object],
) -> None:
    with pytest.raises(StrategyForecastFailure) as error:
        normalize_runner_result(raw)

    assert error.value.code is StrategyForecastErrorCode.STRATEGY_TARGET_INVALID


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"value": object()},
        {"value": float("inf")},
        {"value": "x" * (64 * 1024)},
    ],
)
def test_runner_result_rejects_non_json_or_oversized_diagnostics(
    diagnostics: dict[str, object],
) -> None:
    with pytest.raises(StrategyForecastFailure) as error:
        normalize_runner_result(
            {
                "low_strong": 1,
                "low_watch": 2,
                "high_watch": 3,
                "high_strong": 4,
                "diagnostics": diagnostics,
            }
        )

    assert error.value.code is StrategyForecastErrorCode.STRATEGY_TARGET_INVALID


def test_runner_payload_reports_missing_required_market_field() -> None:
    request = _request()
    rows = tuple(
        {key: value for key, value in row.items() if key != "volume"}
        for row in request.training_data.rows
    )
    request = request.model_copy(
        update={
            "training_data": request.training_data.model_copy(update={"rows": rows})
        }
    )

    with pytest.raises(StrategyForecastFailure) as error:
        build_runner_payload(
            source_code=SOURCE,
            request=request,
            context=_context(request),
            schema={
                "type": "object",
                "properties": {"spread": {"type": "number", "exclusiveMinimum": 0}},
                "required": ["spread"],
                "additionalProperties": False,
            },
        )

    assert error.value.code is StrategyForecastErrorCode.TRAINING_DATA_INVALID


def test_runner_result_turns_hostile_diagnostic_object_into_stable_failure() -> None:
    class HostileDiagnostic:
        @property
        def item(self) -> object:
            raise RuntimeError("must not execute arbitrary diagnostic properties")

    with pytest.raises(StrategyForecastFailure) as error:
        normalize_runner_result(
            {
                "low_strong": 1,
                "low_watch": 2,
                "high_watch": 3,
                "high_strong": 4,
                "diagnostics": {"value": HostileDiagnostic()},
            }
        )

    assert error.value.code is StrategyForecastErrorCode.STRATEGY_TARGET_INVALID
