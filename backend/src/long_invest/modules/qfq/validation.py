import hashlib
import json
from collections.abc import Iterable
from decimal import Decimal

from .contracts import (
    QfqBarInput,
    QfqValidationError,
    RefreshQfq,
    ValidatedQfqWindow,
)


def _canonical_decimal(value: Decimal) -> str:
    parts = value.as_tuple()
    digits = "".join(str(digit) for digit in parts.digits)
    if not digits.strip("0"):
        return "0"

    exponent = parts.exponent
    while digits.endswith("0"):
        digits = digits[:-1]
        exponent += 1

    decimal_at = len(digits) + exponent
    if decimal_at <= 0:
        rendered = f"0.{('0' * -decimal_at)}{digits}"
    elif decimal_at < len(digits):
        rendered = f"{digits[:decimal_at]}.{digits[decimal_at:]}"
    else:
        rendered = f"{digits}{'0' * (decimal_at - len(digits))}"
    return f"-{rendered}" if parts.sign else rendered


def _checksum(bars: tuple[QfqBarInput, ...]) -> str:
    rows = [
        {
            "amount": _canonical_decimal(item.amount),
            "close": _canonical_decimal(item.close),
            "high": _canonical_decimal(item.high),
            "low": _canonical_decimal(item.low),
            "open": _canonical_decimal(item.open),
            "trade_date": item.trade_date.isoformat(),
            "volume": item.volume,
        }
        for item in bars
    ]
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_qfq_window(
    command: RefreshQfq,
    bars: Iterable[QfqBarInput],
    expected_daily_close: Decimal,
) -> ValidatedQfqWindow:
    ordered = tuple(bars)
    if not ordered:
        raise QfqValidationError("QFQ_EMPTY_RESULT")
    if any(
        current.trade_date < previous.trade_date
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ):
        raise QfqValidationError("QFQ_DATE_ORDER_INVALID")
    if len({item.trade_date for item in ordered}) != len(ordered):
        raise QfqValidationError("QFQ_DUPLICATE_DATE")
    if (
        any(
            item.trade_date < command.start or item.trade_date > command.end
            for item in ordered
        )
        or ordered[-1].trade_date != command.end
    ):
        raise QfqValidationError("QFQ_WINDOW_INCOMPLETE")
    if (
        not isinstance(expected_daily_close, Decimal)
        or not expected_daily_close.is_finite()
        or ordered[-1].close != expected_daily_close
    ):
        raise QfqValidationError("QFQ_ADJUSTMENT_BASIS_MISMATCH")

    anchor = ordered[-1]
    return ValidatedQfqWindow(
        bars=ordered,
        anchor_date=command.end,
        anchor_close=anchor.close,
        row_count=len(ordered),
        checksum=_checksum(ordered),
    )
