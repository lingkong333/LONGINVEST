from __future__ import annotations

import ast
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

ALLOWED_IMPORTS = frozenset(
    {
        "collections",
        "datetime",
        "decimal",
        "functools",
        "itertools",
        "math",
        "numpy",
        "operator",
        "pandas",
        "statistics",
        "typing",
    }
)
FORBIDDEN_CALLS = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "input",
        "locals",
        "open",
        "setattr",
        "vars",
    }
)
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "connect",
        "fork",
        "popen",
        "read_clipboard",
        "read_csv",
        "read_excel",
        "read_feather",
        "read_fwf",
        "read_hdf",
        "read_html",
        "read_json",
        "read_orc",
        "read_parquet",
        "read_pickle",
        "read_sas",
        "read_sql",
        "read_stata",
        "read_table",
        "spawn",
        "system",
        "to_clipboard",
        "to_csv",
        "to_excel",
        "to_feather",
        "to_hdf",
        "to_json",
        "to_orc",
        "to_parquet",
        "to_pickle",
        "urlopen",
    }
)
MAX_CONSTANT_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 256 * 1024


class StrategyStaticAnalysisError(ValueError):
    def __init__(self, code: str, message: str, *, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.line = line


@dataclass(frozen=True, slots=True)
class StrategyStaticAnalysis:
    api_version: str
    metadata: MappingProxyType[str, Any]
    parameter_schema: MappingProxyType[str, Any]


def analyze_strategy_source(source: str) -> StrategyStaticAnalysis:
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise StrategyStaticAnalysisError("SOURCE_TOO_LARGE", "source exceeds 256 KB")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise StrategyStaticAnalysisError(
            "PYTHON_SYNTAX_INVALID",
            "strategy source is not valid Python",
            line=exc.lineno,
        ) from exc

    _validate_imports_and_capabilities(tree)
    _validate_constants(tree)
    _validate_entrypoint(tree)
    api_version = _literal_assignment(tree, "STRATEGY_API_VERSION")
    metadata = _literal_assignment(tree, "STRATEGY_META")
    if api_version != "1.0":
        raise StrategyStaticAnalysisError(
            "API_VERSION_INVALID", "STRATEGY_API_VERSION must be 1.0"
        )
    if not isinstance(metadata, dict):
        raise StrategyStaticAnalysisError(
            "METADATA_INVALID", "STRATEGY_META must be a literal object"
        )
    _validate_metadata(metadata)
    schema = metadata["parameter_schema"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise StrategyStaticAnalysisError(
            "PARAMETER_SCHEMA_INVALID", "parameter_schema is not valid JSON Schema"
        ) from exc
    return StrategyStaticAnalysis(
        api_version=api_version,
        metadata=MappingProxyType(metadata),
        parameter_schema=MappingProxyType(schema),
    )


def _validate_imports_and_capabilities(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported = (node.module or "",)
        else:
            imported = ()
        for module_name in imported:
            if module_name.split(".", maxsplit=1)[0] not in ALLOWED_IMPORTS:
                raise StrategyStaticAnalysisError(
                    "IMPORT_FORBIDDEN",
                    f"import is not allowed: {module_name}",
                    line=getattr(node, "lineno", None),
                )
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in FORBIDDEN_CALLS or name in FORBIDDEN_ATTRIBUTES:
                raise StrategyStaticAnalysisError(
                    "DANGEROUS_CAPABILITY",
                    f"dangerous call is not allowed: {name}",
                    line=getattr(node, "lineno", None),
                )
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in FORBIDDEN_CALLS
        ):
            raise StrategyStaticAnalysisError(
                "DANGEROUS_CAPABILITY",
                f"dangerous capability is not allowed: {node.id}",
                line=getattr(node, "lineno", None),
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise StrategyStaticAnalysisError(
                "DANGEROUS_CAPABILITY",
                "dunder attribute access is not allowed",
                line=getattr(node, "lineno", None),
            )


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _validate_constants(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(
            node.value, (str, bytes)
        ):
            continue
        size = len(
            node.value.encode("utf-8") if isinstance(node.value, str) else node.value
        )
        if size > MAX_CONSTANT_BYTES:
            raise StrategyStaticAnalysisError(
                "CONSTANT_TOO_LARGE",
                "strategy constant exceeds 64 KB",
                line=getattr(node, "lineno", None),
            )


def _validate_entrypoint(tree: ast.Module) -> None:
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "calculate_targets"
    ]
    if not functions:
        raise StrategyStaticAnalysisError(
            "ENTRYPOINT_MISSING", "calculate_targets entrypoint is required"
        )
    function = functions[0]
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    valid = (
        isinstance(function, ast.FunctionDef)
        and len(functions) == 1
        and [argument.arg for argument in positional]
        == ["history", "params", "context"]
        and not args.kwonlyargs
        and args.vararg is None
        and args.kwarg is None
        and not args.defaults
        and not function.decorator_list
    )
    if not valid:
        raise StrategyStaticAnalysisError(
            "ENTRYPOINT_SIGNATURE_INVALID",
            "calculate_targets must accept exactly history, params, context",
            line=function.lineno,
        )


def _literal_assignment(tree: ast.Module, name: str) -> Any:
    assignments = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and _assigned_name(node) == name
    ]
    if len(assignments) != 1:
        raise StrategyStaticAnalysisError(
            "METADATA_INVALID", f"{name} must be assigned exactly once"
        )
    try:
        return ast.literal_eval(assignments[0].value)
    except (TypeError, ValueError) as exc:
        raise StrategyStaticAnalysisError(
            "METADATA_INVALID", f"{name} must contain only literal values"
        ) from exc


def _assigned_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    if isinstance(node, ast.AnnAssign):
        return node.target.id if isinstance(node.target, ast.Name) else None
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
        return None
    return node.targets[0].id


def _validate_metadata(metadata: dict[str, Any]) -> None:
    if set(metadata) != {"name", "data_requirements", "parameter_schema"}:
        raise StrategyStaticAnalysisError(
            "METADATA_INVALID", "STRATEGY_META has unexpected fields"
        )
    requirements = metadata.get("data_requirements")
    if not isinstance(metadata.get("name"), str) or not metadata["name"].strip():
        raise StrategyStaticAnalysisError(
            "METADATA_INVALID", "strategy name is required"
        )
    if not isinstance(requirements, dict) or set(requirements) != {
        "adjustment",
        "min_bars",
        "max_bars",
    }:
        raise StrategyStaticAnalysisError(
            "METADATA_INVALID", "data_requirements is invalid"
        )
    min_bars = requirements.get("min_bars")
    max_bars = requirements.get("max_bars")
    if (
        requirements.get("adjustment") != "qfq"
        or not isinstance(min_bars, int)
        or isinstance(min_bars, bool)
        or not isinstance(max_bars, int)
        or isinstance(max_bars, bool)
        or min_bars < 1
        or max_bars < min_bars
    ):
        raise StrategyStaticAnalysisError(
            "METADATA_INVALID", "data_requirements values are invalid"
        )
    if not isinstance(metadata.get("parameter_schema"), dict):
        raise StrategyStaticAnalysisError(
            "PARAMETER_SCHEMA_INVALID", "parameter_schema must be an object"
        )
