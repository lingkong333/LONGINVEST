from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from long_invest.modules.providers.contracts import RealtimeQuote


MAX_FRESHNESS_SECONDS = 180


@dataclass(frozen=True, slots=True)
class QuoteValidation:
    valid: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class QuoteComparison:
    conflict: bool
    difference: Decimal
    threshold: Decimal


def validate_quote(
    quote: RealtimeQuote, *, symbol: str, now: datetime
) -> QuoteValidation:
    _require_aware(now)
    if quote.symbol != symbol:
        return QuoteValidation(False, "QUOTE_SYMBOL_MISMATCH")
    if quote.price <= 0:
        return QuoteValidation(False, "QUOTE_PRICE_INVALID")
    prices = (quote.open, quote.high, quote.low, quote.price)
    if any(value <= 0 for value in prices):
        return QuoteValidation(False, "QUOTE_OHLC_INVALID")
    if quote.high < max(prices) or quote.low > min(prices):
        return QuoteValidation(False, "QUOTE_OHLC_INVALID")
    if quote.previous_close < 0 or quote.volume < 0 or quote.amount < 0:
        return QuoteValidation(False, "QUOTE_QUANTITY_INVALID")
    if not _is_aware(quote.quote_time) or not _is_aware(quote.received_at):
        return QuoteValidation(False, "QUOTE_TIME_INVALID")
    quote_time = quote.quote_time.astimezone(UTC)
    server_time = now.astimezone(UTC)
    if quote_time > server_time:
        return QuoteValidation(False, "QUOTE_TIME_FUTURE")
    if (server_time - quote_time).total_seconds() > MAX_FRESHNESS_SECONDS:
        return QuoteValidation(False, "QUOTE_STALE")
    return QuoteValidation(True)


def compare_quotes(
    primary: RealtimeQuote, fallback: RealtimeQuote
) -> QuoteComparison:
    difference = abs(primary.price - fallback.price)
    threshold = max(
        Decimal("0.02"),
        max(primary.price, fallback.price) * Decimal("0.002"),
    )
    return QuoteComparison(
        conflict=difference > threshold,
        difference=difference,
        threshold=threshold,
    )


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _require_aware(value: datetime) -> None:
    if not _is_aware(value):
        raise ValueError("datetime must include timezone")
