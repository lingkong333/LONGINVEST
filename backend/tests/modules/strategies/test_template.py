from long_invest.modules.strategies.runner_execution import execute_runner_payload
from long_invest.modules.strategies.static_analysis import analyze_strategy_source
from long_invest.modules.strategies.template import (
    DEFAULT_STRATEGY_SOURCE,
    default_strategy_metadata,
)
from long_invest.platform.json_snapshot import thaw_json_value


def test_default_template_matches_the_enforced_strategy_contract() -> None:
    analysis = analyze_strategy_source(DEFAULT_STRATEGY_SOURCE)

    assert thaw_json_value(analysis.parameter_schema) == default_strategy_metadata()[
        "parameter_schema"
    ]


def test_default_template_executes_as_a_safe_non_matching_strategy() -> None:
    history = [
        {
            "trade_date": f"2026-01-{day:02d}",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10,
            "volume": 100,
            "amount": 1000,
        }
        for day in range(1, 29)
    ] + [
        {
            "trade_date": f"2026-02-{day:02d}",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10,
            "volume": 100,
            "amount": 1000,
        }
        for day in range(1, 29)
    ] + [
        {
            "trade_date": f"2026-03-{day:02d}",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10,
            "volume": 100,
            "amount": 1000,
        }
        for day in range(1, 5)
    ]

    result = execute_runner_payload(
        {
            "source_code": DEFAULT_STRATEGY_SOURCE,
            "parameters": {"price_band_ratio": 0.05},
            "context": {"symbol": "600000.SH"},
            "history": history,
        }
    )

    assert result == {
        "matched": False,
        "reason": "策略逻辑尚未实现",
        "diagnostics": {"history_rows": 60, "symbol": "600000.SH"},
    }
