from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, DecimalException
from typing import Any

import numpy as np
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from long_invest.modules.strategies.contracts import (
    StrategyForecastErrorCode,
    StrategyForecastRequest,
    StrategyForecastResult,
)
from long_invest.modules.strategies.static_analysis import analyze_strategy_source
from long_invest.modules.targets.contracts import TargetValues
from long_invest.platform.json_snapshot import thaw_json_value

HISTORY_COLUMNS = (
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
CONTEXT_FIELDS = frozenset(
    {
        "symbol",
        "exchange",
        "name",
        "as_of_date",
        "strategy_version_id",
        "data_version",
        "calculation_reason",
    }
)
MAX_DIAGNOSTICS_BYTES = 64 * 1024


class StrategyForecastFailure(RuntimeError):
    def __init__(self, code: StrategyForecastErrorCode | str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_runner_payload(
    *,
    source_code: str,
    request: StrategyForecastRequest,
    context: Mapping[str, object],
    schema: Mapping[str, object],
) -> dict[str, Any]:
    normalized_context = _normalize_context(request, context)
    analysis = analyze_strategy_source(source_code)
    schema_value = thaw_json_value(schema)
    if schema_value != thaw_json_value(analysis.parameter_schema):
        raise StrategyForecastFailure(
            "STRATEGY_PARAMETER_SCHEMA_MISMATCH",
            "frozen parameter schema does not match strategy source",
        )
    parameters = thaw_json_value(request.parameter_snapshot)
    errors = sorted(
        Draft202012Validator(schema_value).iter_errors(parameters),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise StrategyForecastFailure(
            "STRATEGY_PARAMETER_INVALID", "strategy parameters do not match schema"
        )
    try:
        rows = [
            {column: thaw_json_value(row[column]) for column in HISTORY_COLUMNS}
            for row in request.training_data.rows
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyForecastFailure(
            StrategyForecastErrorCode.TRAINING_DATA_INVALID,
            "training data is missing required market fields",
        ) from exc
    return {
        "source_code": source_code,
        "parameters": parameters,
        "context": normalized_context,
        "history": rows,
    }


def normalize_runner_result(raw_result: object) -> StrategyForecastResult:
    if not isinstance(raw_result, Mapping):
        raise _invalid_result()
    allowed = {"low_strong", "low_watch", "high_watch", "high_strong", "diagnostics"}
    if set(raw_result) - allowed or not allowed.difference({"diagnostics"}) <= set(
        raw_result
    ):
        raise _invalid_result()
    try:
        values = TargetValues(
            **{
                key: Decimal(str(raw_result[key]))
                for key in ("low_strong", "low_watch", "high_watch", "high_strong")
            }
        )
    except (DecimalException, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise _invalid_result() from exc
    diagnostics = raw_result.get("diagnostics", {})
    if not isinstance(diagnostics, Mapping):
        raise _invalid_result()
    try:
        normalized_diagnostics = _normalize_json_value(diagnostics)
        encoded = json.dumps(
            normalized_diagnostics,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _invalid_result() from exc
    if len(encoded) > MAX_DIAGNOSTICS_BYTES:
        raise _invalid_result()
    return StrategyForecastResult(values=values, diagnostics=normalized_diagnostics)


def _normalize_context(
    request: StrategyForecastRequest, context: Mapping[str, object]
) -> dict[str, Any]:
    if set(context) != CONTEXT_FIELDS:
        raise StrategyForecastFailure(
            StrategyForecastErrorCode.TEST_DATA_EXPOSED_TO_STRATEGY,
            "strategy context contains forbidden fields",
        )
    expected = {
        "symbol": request.training_data.symbol,
        "exchange": request.training_data.symbol.rsplit(".", maxsplit=1)[1],
        "strategy_version_id": str(request.strategy_version_id),
        "data_version": request.training_data.data_version,
    }
    if any(context[key] != value for key, value in expected.items()):
        raise StrategyForecastFailure(
            StrategyForecastErrorCode.TEST_DATA_EXPOSED_TO_STRATEGY,
            "strategy context does not match frozen training data",
        )
    as_of_date = context["as_of_date"]
    if not isinstance(as_of_date, date) or as_of_date != request.training_data.end_date:
        raise StrategyForecastFailure(
            StrategyForecastErrorCode.TEST_DATA_EXPOSED_TO_STRATEGY,
            "strategy context exceeds the training period",
        )
    if not isinstance(context["name"], str) or not context["name"].strip():
        raise StrategyForecastFailure("STRATEGY_CONTEXT_INVALID", "name is required")
    if (
        not isinstance(context["calculation_reason"], str)
        or not context["calculation_reason"].strip()
    ):
        raise StrategyForecastFailure(
            "STRATEGY_CONTEXT_INVALID", "calculation reason is required"
        )
    normalized = dict(context)
    normalized["as_of_date"] = as_of_date.isoformat()
    return _normalize_json_value(normalized)


def _normalize_json_value(value: object) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not Decimal(str(value)).is_finite():
            raise ValueError("non-finite JSON number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, np.generic):
        return _normalize_json_value(value.item())
    raise TypeError("value is not JSON-compatible")


def _invalid_result() -> StrategyForecastFailure:
    return StrategyForecastFailure(
        StrategyForecastErrorCode.STRATEGY_TARGET_INVALID,
        "strategy returned invalid targets or diagnostics",
    )
