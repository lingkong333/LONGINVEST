from types import MappingProxyType

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
    assert result.parameter_schema["required"] == ("window",)


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


@pytest.mark.parametrize(
    "reference",
    [
        {"$ref": "https://attacker.invalid/schema.json"},
        {"$ref": "file:///etc/passwd"},
        {"$dynamicRef": "#node"},
        {"$recursiveRef": "#"},
    ],
)
def test_static_analysis_rejects_all_schema_references(
    reference: dict[str, str],
) -> None:
    source = VALID_SOURCE.replace(
        '"properties": {"window": {"type": "integer", "minimum": 1}},',
        f'"properties": {{"window": {reference!r}}},',
    )

    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == "PARAMETER_SCHEMA_REFERENCE_FORBIDDEN"


@pytest.mark.parametrize(
    "dangerous_source",
    [
        "from pandas import read_csv as loader\nloader('/tmp/value')",
        "import pandas as safe_name\nsafe_name.read_csv('/tmp/value')",
        "import pandas as safe_name\nloader = safe_name.read_csv\nloader('/tmp/value')",
        "danger = open\nindirect = danger\nindirect('/tmp/value')",
        "from pandas import __builtins__ as harmless\nharmless['open']('/tmp/value')",
    ],
)
def test_static_analysis_rejects_dangerous_aliases(dangerous_source: str) -> None:
    source = VALID_SOURCE.replace(
        "def calculate_targets(history, params, context):",
        f"{dangerous_source}\ndef calculate_targets(history, params, context):",
    )

    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == "DANGEROUS_CAPABILITY"


@pytest.mark.parametrize(
    "rebind",
    [
        "calculate_targets = lambda history, params, context: {}",
        "calculate_targets, other = other, calculate_targets",
        "del calculate_targets",
        "import math as calculate_targets",
        (
            "if True:\n"
            "    def calculate_targets(history, params, context):\n"
            "        return {}"
        ),
    ],
)
def test_static_analysis_rejects_entrypoint_rebinding(rebind: str) -> None:
    source = VALID_SOURCE + f"\n{rebind}\n"

    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == "ENTRYPOINT_REBOUND"


def test_static_analysis_deeply_freezes_metadata_and_schema() -> None:
    result = analyze_strategy_source(VALID_SOURCE)

    assert isinstance(result.metadata, MappingProxyType)
    assert isinstance(result.metadata["data_requirements"], MappingProxyType)
    assert isinstance(result.parameter_schema["properties"], MappingProxyType)
    with pytest.raises(TypeError):
        result.metadata["data_requirements"]["min_bars"] = 1
    with pytest.raises(TypeError):
        result.parameter_schema["properties"]["window"] = {"type": "string"}


@pytest.mark.parametrize(
    "dangerous_source",
    [
        "np.load('/tmp/value.npy')",
        "np.save('/tmp/value.npy', [])",
        "np.fromfile('/tmp/value')",
        "np.loadtxt('/tmp/value')",
        "np.savetxt('/tmp/value', [])",
        "np.lib.npyio.DataSource().open('/tmp/value')",
        "np.lib.format.open_memmap('/tmp/value')",
        "history['close'].tofile('/tmp/value')",
        "np.memmap('/tmp/value')",
        "np.ctypeslib.load_library('value', '/tmp')",
        "pd.read_secret('/tmp/value')",
        "pd.ExcelFile('/tmp/value')",
        "pd.HDFStore('/tmp/value')",
        "pd.io.common.os.system('id')",
        "np.lib.npyio.os.listdir('/tmp')",
        "from numpy import load as loader\nloader('/tmp/value')",
        "from pandas import io as pandas_io\npandas_io.common.urlopen('https://x')",
        "import numpy.ctypeslib as native\nnative.load_library('x', '/tmp')",
        "import operator\noperator.attrgetter('io.common.os.system')(pd)('id')",
        "from numpy.lib._datasource import open as reader\nreader('/tmp/value')",
        "import numpy.lib._datasource as ds\nreader = ds.open\nreader('/tmp/value')",
        "history.to_xml('/tmp/value.xml')",
        "history.to_numpy().dump('/tmp/value.npy')",
    ],
)
def test_static_analysis_rejects_dangerous_library_capability_chains(
    dangerous_source: str,
) -> None:
    source = VALID_SOURCE.replace(
        "def calculate_targets(history, params, context):",
        f"{dangerous_source}\ndef calculate_targets(history, params, context):",
    )

    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == "DANGEROUS_CAPABILITY"


def test_static_analysis_keeps_normal_pandas_numpy_calculation_available() -> None:
    source = VALID_SOURCE.replace(
        "close = Decimal(str(history",
        (
            "values = history.to_numpy()\n"
            "    mean = np.mean(values)\n"
            "    rolling = history['close'].rolling(2).mean()\n"
            "    assert pd.notna(mean)\n"
            "    close = Decimal(str(history"
        ),
    )

    result = analyze_strategy_source(source)

    assert result.api_version == "1.0"


@pytest.mark.parametrize(
    "rebind",
    [
        "match object():\n    case calculate_targets:\n        pass",
        "match []:\n    case [*calculate_targets]:\n        pass",
        "def other(calculate_targets):\n    return calculate_targets",
        "alias = lambda calculate_targets: calculate_targets",
        "def other[calculate_targets]():\n    return None",
    ],
)
def test_static_analysis_rejects_all_scope_entrypoint_bindings(rebind: str) -> None:
    source = VALID_SOURCE + f"\n{rebind}\n"

    with pytest.raises(StrategyStaticAnalysisError) as error:
        analyze_strategy_source(source)

    assert error.value.code == "ENTRYPOINT_REBOUND"
