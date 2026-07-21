from __future__ import annotations

import builtins
from collections.abc import Mapping
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
    analyze_strategy_source(source_code)
    frame = pd.DataFrame.from_records(history, columns=HISTORY_COLUMNS)
    if frame.empty:
        raise ValueError("training history is empty")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise")
    for column in HISTORY_COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    if not frame["trade_date"].is_monotonic_increasing:
        raise ValueError("training history is not ordered")

    namespace: dict[str, Any] = {"__builtins__": _safe_builtins()}
    exec(compile(source_code, "<strategy>", "exec"), namespace, namespace)
    calculate_targets = namespace["calculate_targets"]
    return calculate_targets(frame, dict(parameters), dict(context))


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
