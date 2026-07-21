from __future__ import annotations

import builtins
import json
import math
import time
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any

import pandas as pd

from long_invest.modules.strategies.forecast import HISTORY_COLUMNS
from long_invest.modules.strategies.static_analysis import (
    ALLOWED_IMPORTS,
    analyze_strategy_source,
)

PAYLOAD_FIELDS = frozenset({"source_code", "parameters", "context", "history"})


def execute_runner_payload(payload: Mapping[str, object]) -> object:
    if set(payload) != PAYLOAD_FIELDS:
        raise ValueError("runner payload shape is invalid")
    source_code = payload["source_code"]
    parameters = payload["parameters"]
    context = payload["context"]
    history = payload["history"]
    if (
        not isinstance(source_code, str)
        or not isinstance(parameters, Mapping)
        or not isinstance(context, Mapping)
        or not isinstance(history, list)
    ):
        raise ValueError("runner payload values are invalid")
    analysis = analyze_strategy_source(source_code)
    frame = _validated_history_frame(history)
    requirements = analysis.metadata["data_requirements"]
    if not int(requirements["min_bars"]) <= len(frame) <= int(
        requirements["max_bars"]
    ):
        raise ValueError("training history does not satisfy strategy bar limits")

    namespace: dict[str, Any] = {"__builtins__": _safe_builtins()}
    exec(compile(source_code, "<strategy>", "exec"), namespace, namespace)
    calculate_targets = namespace["calculate_targets"]
    return calculate_targets(frame, dict(parameters), dict(context))


def wait_for_runner_payload(
    input_path: str | PathLike[str],
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.01,
    sleep: Any = time.sleep,
) -> Mapping[str, object]:
    path = Path(input_path)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            raw_payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise TimeoutError("runner input was not provided") from None
            sleep(poll_interval_seconds)
            continue
        if not isinstance(raw_payload, Mapping):
            raise ValueError("runner payload shape is invalid")
        return raw_payload


def _validated_history_frame(history: list[object]) -> pd.DataFrame:
    if not history or any(
        not isinstance(row, Mapping) or set(row) != set(HISTORY_COLUMNS)
        for row in history
    ):
        raise ValueError("training history columns are invalid")
    frame = pd.DataFrame.from_records(history, columns=HISTORY_COLUMNS)
    try:
        frame["trade_date"] = pd.to_datetime(
            frame["trade_date"], format="%Y-%m-%d", errors="raise"
        )
        for column in HISTORY_COLUMNS[1:]:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("training history values are invalid") from exc
    if (
        not frame["trade_date"].is_monotonic_increasing
        or frame["trade_date"].duplicated().any()
        or any(
            not math.isfinite(float(value))
            for column in HISTORY_COLUMNS[1:]
            for value in frame[column]
        )
    ):
        raise ValueError("training history is not unique, ordered, and finite")
    price_columns = ["open", "high", "low", "close"]
    if (
        (frame[price_columns] <= 0).any().any()
        or (frame["low"] > frame["high"]).any()
        or (frame["open"] < frame["low"]).any()
        or (frame["open"] > frame["high"]).any()
        or (frame["close"] < frame["low"]).any()
        or (frame["close"] > frame["high"]).any()
        or (frame[["volume", "amount"]] < 0).any().any()
    ):
        raise ValueError("training history market values are invalid")
    return frame


def _safe_builtins() -> dict[str, object]:
    allowed_names = {
        "ArithmeticError",
        "AssertionError",
        "Exception",
        "ValueError",
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "pow",
        "range",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
    values = {name: getattr(builtins, name) for name in allowed_names}
    values["__import__"] = _safe_import
    return values


def _safe_import(
    name: str,
    globals_: Mapping[str, object] | None = None,
    locals_: Mapping[str, object] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> object:
    if level != 0 or name.split(".", maxsplit=1)[0] not in ALLOWED_IMPORTS:
        raise ImportError("strategy import is not allowed")
    return builtins.__import__(name, globals_, locals_, fromlist, level)
