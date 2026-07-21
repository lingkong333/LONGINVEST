from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, DecimalException
from hashlib import sha256
from typing import Any
from uuid import UUID

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
INPUT_HASH_MISMATCH = "STRATEGY_INPUT_HASH_MISMATCH"
PARAMETER_SCHEMA_MISMATCH = "STRATEGY_PARAMETER_SCHEMA_MISMATCH"
PARAMETER_INVALID = "STRATEGY_PARAMETER_INVALID"
CONTEXT_INVALID = "STRATEGY_CONTEXT_INVALID"


class StrategyForecastFailure(RuntimeError):
    def __init__(self, code: StrategyForecastErrorCode | str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_runner_payload(
    *,
    request: StrategyForecastRequest,
    context: Mapping[str, object],
    source_code: str | None = None,
    schema: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    normalized_context = _normalize_context(request, context)
    if source_code is not None and source_code != request.source_code:
        raise StrategyForecastFailure(
            INPUT_HASH_MISMATCH, "strategy source differs from frozen request"
        )
    if schema is not None and thaw_json_value(schema) != thaw_json_value(
        request.parameter_schema
    ):
        raise StrategyForecastFailure(
            PARAMETER_SCHEMA_MISMATCH,
            "parameter schema differs from frozen request",
        )
    source_code = request.source_code
    analysis = analyze_strategy_source(source_code)
    parameters = thaw_json_value(request.parameter_snapshot)
    if (
        hash_source_code(source_code) != request.source_code_hash
        or hash_parameter_snapshot(parameters) != request.parameter_hash
    ):
        raise StrategyForecastFailure(
            INPUT_HASH_MISMATCH, "strategy source or parameter snapshot changed"
        )
    schema_value = thaw_json_value(request.parameter_schema)
    if schema_value != thaw_json_value(analysis.parameter_schema):
        raise StrategyForecastFailure(
            PARAMETER_SCHEMA_MISMATCH,
            "frozen parameter schema does not match strategy source",
        )
    errors = sorted(
        Draft202012Validator(schema_value).iter_errors(parameters),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise StrategyForecastFailure(
            PARAMETER_INVALID, "strategy parameters do not match schema"
        )
    rows = _validate_training_rows(request.training_data)
    if hash_training_data_snapshot(request.training_data) != (
        request.training_data.content_hash
    ):
        raise StrategyForecastFailure(
            INPUT_HASH_MISMATCH, "training data snapshot changed"
        )
    requirements = analysis.metadata["data_requirements"]
    min_bars = int(requirements["min_bars"])
    max_bars = int(requirements["max_bars"])
    if not min_bars <= len(rows) <= max_bars:
        raise StrategyForecastFailure(
            StrategyForecastErrorCode.INSUFFICIENT_HISTORY,
            "training history does not satisfy strategy bar limits",
        )
    return {
        "source_code": source_code,
        "parameters": parameters,
        "context": normalized_context,
        "history": rows,
    }


def hash_source_code(source_code: str) -> str:
    return sha256(source_code.encode("utf-8")).hexdigest()


def hash_parameter_snapshot(parameters: Mapping[str, object]) -> str:
    return sha256(_canonical_json_bytes(parameters)).hexdigest()


def hash_training_data_snapshot(training_data: object) -> str:
    required = (
        "security_id",
        "symbol",
        "start_date",
        "end_date",
        "data_version",
        "fetched_at",
        "source",
        "price_basis",
        "rows",
    )
    try:
        snapshot = {name: getattr(training_data, name) for name in required}
    except AttributeError as exc:
        raise ValueError("training snapshot is incomplete") from exc
    return sha256(_canonical_json_bytes(snapshot)).hexdigest()


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
        "strategy_version_id": str(
            request.strategy_version_id or request.draft_id
        ),
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
            CONTEXT_INVALID, "calculation reason is required"
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


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_json_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_json_value(value: object) -> Any:
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _canonical_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return _normalize_json_value(value)


def _validate_training_rows(training_data: object) -> list[dict[str, object]]:
    try:
        start_date = training_data.start_date
        end_date = training_data.end_date
        raw_rows = training_data.rows
    except AttributeError as exc:
        raise _training_data_invalid("training snapshot is incomplete") from exc
    if (
        type(start_date) is not date
        or type(end_date) is not date
        or start_date > end_date
    ):
        raise _training_data_invalid("training date range is invalid")
    rows: list[dict[str, object]] = []
    dates: list[date] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping) or set(raw_row) != set(HISTORY_COLUMNS):
            raise _training_data_invalid("training data columns are invalid")
        trade_date = raw_row["trade_date"]
        if type(trade_date) is not date or not start_date <= trade_date <= end_date:
            raise _training_data_invalid("training row date is invalid")
        values = {
            column: _finite_decimal(raw_row[column]) for column in HISTORY_COLUMNS[1:]
        }
        low = values["low"]
        high = values["high"]
        open_ = values["open"]
        close = values["close"]
        if (
            min(low, high, open_, close) <= 0
            or low > high
            or not low <= open_ <= high
            or not low <= close <= high
            or values["volume"] < 0
            or values["amount"] < 0
        ):
            raise _training_data_invalid("training row market values are invalid")
        dates.append(trade_date)
        rows.append(
            {
                "trade_date": trade_date.isoformat(),
                **{
                    column: thaw_json_value(raw_row[column])
                    for column in HISTORY_COLUMNS[1:]
                },
            }
        )
    if not rows or dates != sorted(dates) or len(set(dates)) != len(dates):
        raise _training_data_invalid("training dates must be unique and increasing")
    return rows


def _finite_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise _training_data_invalid("training numeric values are invalid")
    try:
        number = Decimal(str(value))
    except (DecimalException, TypeError, ValueError) as exc:
        raise _training_data_invalid("training numeric values are invalid") from exc
    if not number.is_finite():
        raise _training_data_invalid("training numeric values must be finite")
    return number


def _training_data_invalid(message: str) -> StrategyForecastFailure:
    return StrategyForecastFailure(
        StrategyForecastErrorCode.TRAINING_DATA_INVALID,
        message,
    )


def _invalid_result() -> StrategyForecastFailure:
    return StrategyForecastFailure(
        StrategyForecastErrorCode.STRATEGY_TARGET_INVALID,
        "strategy returned invalid targets or diagnostics",
    )
