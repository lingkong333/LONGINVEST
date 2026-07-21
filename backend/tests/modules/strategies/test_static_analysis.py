import pytest

from long_invest.modules.strategies.static_analysis import (
    StrategyStaticAnalysisError,
    analyze_strategy_source,
)

VALID_SOURCE = """
import math
import numpy as np
import pandas as pd
from decimal import Decimal

STRATEGY_API_VERSION = "1.0"
STRATEGY_META = {
    "name": "test strategy",
    "data_requirements": {
        "adjustment": "qfq",
        "min_bars": 2,
        "max_bars": 1000,
    },
    "parameter_schema": {
        "type": "object",
        "properties": {"window": {"type": "integer", "minimum": 1}},
        "required": ["window"],
        "additionalProperties": False,
    },
}

def calculate_targets(history, params, context):
    close = Decimal(str(history["close"].iloc[-1]))
    return {
        "low_strong": close * Decimal("0.8"),
        "low_watch": close * Decimal("0.9"),
        "high_watch": close * Decimal("1.1"),
        "high_strong": close * Decimal("1.2"),
    }
"""


def test_static_analysis_accepts_fixed_contract_and_safe_imports() -> None:
    result = analyze_strategy_source(VALID_SOURCE)

    assert result.api_version == "1.0"
    assert result.metadata["name"] == "test strategy"
    assert result.parameter_schema["required"] == ["window"]


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("def other(history, params, context): pass", "ENTRYPOINT_MISSING"),
        (
            VALID_SOURCE.replace(
                "def calculate_targets(history, params, context):",
                "def calculate_targets(history, test_data, context):",
            ),
            "ENTRYPOINT_SIGNATURE_INVALID",
        ),
        (VALID_SOURCE.replace("import math", "import socket"), "IMPORT_FORBIDDEN"),
        (
            VALID_SOURCE.replace(
                "close = Decimal(str(history",
                "open('/tmp/x')\n    close = Decimal(str(history",
            ),
            "DANGEROUS_CAPABILITY",
        ),
    ],
)
def test_static_analysis_rejects_invalid_or_dangerous_source(
    source: str, code: str
) -> None:
    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == code


def test_static_analysis_rejects_invalid_metadata_schema() -> None:
    source = VALID_SOURCE.replace(
        '"properties": {"window": {"type": "integer", "minimum": 1}},',
        '"properties": {"window": {"type": "not-a-json-type"}},',
    )

    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == "PARAMETER_SCHEMA_INVALID"


def test_static_analysis_rejects_dangerous_builtin_through_alias() -> None:
    source = VALID_SOURCE.replace(
        "def calculate_targets(history, params, context):",
        "danger = open\ndef calculate_targets(history, params, context):",
    )

    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == "DANGEROUS_CAPABILITY"


def test_static_analysis_rejects_oversized_constants() -> None:
    source = VALID_SOURCE + f"\nLARGE = {'x' * 65_537!r}\n"

    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == "CONSTANT_TOO_LARGE"
