from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class DailyQualityContext:
    is_new_listing: bool = False
    is_st: bool = False
    has_known_corporate_action: bool = False
    previous_close: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DailyQualityResult:
    valid: bool
    code: str
    message: str
    review_required: bool = False


_REQUIRED_FIELDS = frozenset(
    {
        "symbol",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "source",
    }
)
_OPTIONAL_FIELDS = frozenset({"previous_close", "capability"})


def validate_daily_bar(
    bar: Mapping[str, object],
    *,
    expected_symbol: str,
    expected_date: date,
    context: DailyQualityContext,
    seen_keys: set[tuple[str, date]] | frozenset[tuple[str, date]] = frozenset(),
) -> DailyQualityResult:
    keys = frozenset(bar)
    if (
        not _REQUIRED_FIELDS.issubset(keys)
        or keys - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
    ):
        return _invalid("DAILY_BAR_SCHEMA_INVALID", "日线字段结构不符合契约")

    if bar["symbol"] != expected_symbol:
        return _invalid("DAILY_BAR_SYMBOL_MISMATCH", "日线代码与请求不一致")
    if bar["trading_date"] != expected_date:
        return _invalid("DAILY_BAR_DATE_MISMATCH", "日线日期与请求不一致")
    if (expected_symbol, expected_date) in seen_keys:
        return _invalid("DAILY_BAR_DUPLICATE", "同一股票交易日出现重复日线")

    try:
        open_ = _decimal(bar["open"])
        high = _decimal(bar["high"])
        low = _decimal(bar["low"])
        close = _decimal(bar["close"])
        amount = _decimal(bar["amount"])
        volume = bar["volume"]
        if isinstance(volume, bool) or not isinstance(volume, int):
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        return _invalid("DAILY_BAR_SCHEMA_INVALID", "日线数值字段类型无效")

    if any(value <= 0 for value in (open_, high, low, close)):
        return _invalid("DAILY_BAR_INVALID", "日线价格必须大于零")
    if high < max(open_, close, low) or low > min(open_, close, high):
        return _invalid("DAILY_BAR_INVALID", "日线高低价格关系无效")
    if volume < 0 or amount < 0:
        return _invalid("DAILY_BAR_INVALID", "日线成交量额不能为负数")
    if volume > 10**12 or amount > Decimal("1000000000000000"):
        return DailyQualityResult(
            valid=True,
            code="DAILY_BAR_MAGNITUDE_ANOMALY",
            message="日线成交量级异常，需要复核",
            review_required=True,
        )

    previous_close = context.previous_close
    if previous_close is not None and previous_close > 0:
        change = abs(close - previous_close) / previous_close
        if change > Decimal("0.30"):
            if (
                context.is_new_listing
                or context.is_st
                or context.has_known_corporate_action
            ):
                return DailyQualityResult(
                    valid=True,
                    code="DAILY_BAR_PREVIOUS_CLOSE_EXPLAINED",
                    message=_context_explanation(context),
                )
            return DailyQualityResult(
                valid=True,
                code="DAILY_BAR_PREVIOUS_CLOSE_ANOMALY",
                message="相对前收盘价格跳变异常，需要复核",
                review_required=True,
            )
    return DailyQualityResult(valid=True, code="OK", message="日线校验通过")


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError
    return result


def _invalid(code: str, message: str) -> DailyQualityResult:
    return DailyQualityResult(valid=False, code=code, message=message)


def _context_explanation(context: DailyQualityContext) -> str:
    reasons: list[str] = []
    if context.is_new_listing:
        reasons.append("新股")
    if context.is_st:
        reasons.append("ST")
    if context.has_known_corporate_action:
        reasons.append("已知公司行为")
    return "价格跳变由" + "、".join(reasons) + "上下文解释"
