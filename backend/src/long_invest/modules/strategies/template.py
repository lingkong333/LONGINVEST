from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_STRATEGY_METADATA: dict[str, Any] = {
    "name": "新策略模板",
    "data_requirements": {
        "adjustment": "qfq",
        "min_bars": 60,
        "max_bars": 5000,
    },
    "parameter_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "price_band_ratio": {
                "type": "number",
                "title": "价格带比例",
                "description": "示例参数，用于控制四档价格相对参考价的间距。",
                "exclusiveMinimum": 0,
                "maximum": 0.2,
                "default": 0.05,
            }
        },
        "required": ["price_band_ratio"],
    },
}

DEFAULT_STRATEGY_SOURCE = '''\
STRATEGY_API_VERSION = "1.0"

# 这段元数据是运行契约的一部分，AI 生成策略时必须保留三个顶层字段。
STRATEGY_META = {
    "name": "新策略模板",
    "data_requirements": {
        # history 只提供前复权日线；min_bars/max_bars 限制输入行数。
        "adjustment": "qfq",
        "min_bars": 60,
        "max_bars": 5000,
    },
    "parameter_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "price_band_ratio": {
                "type": "number",
                "title": "价格带比例",
                "description": "示例参数，用于控制四档价格相对参考价的间距。",
                "exclusiveMinimum": 0,
                "maximum": 0.2,
                "default": 0.05,
            }
        },
        "required": ["price_band_ratio"],
    },
}


def calculate_targets(history, params, context):
    """根据训练期日线判断股票是否匹配，并在匹配时返回四档价格。"""
    # 给 AI 的固定输入说明：
    # 1. history 是按 trade_date 升序排列的 pandas.DataFrame，只包含训练期数据。
    #    固定列为 trade_date/open/high/low/close/volume/amount，禁止读取测试期数据。
    # 2. params 已通过上方 parameter_schema 校验，不允许使用未声明参数。
    # 3. context 只包含 symbol/exchange/name/as_of_date/strategy_version_id/
    #    data_version/calculation_reason。策略不能访问数据库、文件或网络。
    #
    # 不符合条件时必须返回：
    # return {"matched": False, "reason": "明确原因", "diagnostics": {}}
    #
    # 符合条件时必须同时返回下面四档价格，且严格满足：
    # 0 < low_strong < low_watch < high_watch < high_strong
    # return {
    #     "matched": True,
    #     "low_strong": 8.00,
    #     "low_watch": 9.00,
    #     "high_watch": 12.00,
    #     "high_strong": 13.00,
    #     "diagnostics": {"reference_close": 10.00},
    # }
    # diagnostics 只能放 JSON 基本类型，不能返回 DataFrame、日线全集或 NaN。

    # 安全占位：请让 AI 在这里实现真实筛选条件和四档价格计算。
    return {
        "matched": False,
        "reason": "策略逻辑尚未实现",
        "diagnostics": {
            "history_rows": len(history),
            "symbol": context["symbol"],
        },
    }
'''


def default_strategy_metadata() -> dict[str, Any]:
    return deepcopy(DEFAULT_STRATEGY_METADATA)


def default_strategy_parameter_schema() -> dict[str, Any]:
    return deepcopy(DEFAULT_STRATEGY_METADATA["parameter_schema"])
