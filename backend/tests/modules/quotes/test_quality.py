from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from long_invest.modules.providers.contracts import ProviderCode, RealtimeQuote
from long_invest.modules.quotes.quality import (
    QuoteValidation,
    compare_quotes,
    validate_quote,
)

NOW = datetime(2026, 7, 15, 2, 0, tzinfo=UTC)


def quote(price: str = "10.00", **overrides: object) -> RealtimeQuote:
    current = Decimal(price)
    values = {
        "symbol": "600000.SH",
        "price": current,
        "open": min(current, Decimal("9.90")),
        "high": max(current, Decimal("10.10")),
        "low": min(current, Decimal("9.80")),
        "previous_close": Decimal("9.95"),
        "volume": 100,
        "amount": Decimal("1000"),
        "quote_time": NOW - timedelta(seconds=30),
        "received_at": NOW,
        "source": ProviderCode.EASTMONEY,
    }
    values.update(overrides)
    return RealtimeQuote(**values)  # type: ignore[arg-type]


def corrupted(base: RealtimeQuote, **values: object) -> RealtimeQuote:
    result = replace(base)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    return result


def test_valid_quote_is_accepted_at_freshness_boundary() -> None:
    result = validate_quote(
        quote(quote_time=NOW - timedelta(seconds=180)), symbol="600000.SH", now=NOW
    )
    assert result.valid is True
    assert result.error_code is None


@pytest.mark.parametrize(
    ("candidate", "symbol", "code"),
    [
        (quote(), "000001.SZ", "QUOTE_SYMBOL_MISMATCH"),
        (corrupted(quote(), price=Decimal("0")), "600000.SH", "QUOTE_PRICE_INVALID"),
        (corrupted(quote(), high=Decimal("9.00")), "600000.SH", "QUOTE_OHLC_INVALID"),
        (corrupted(quote(), volume=-1), "600000.SH", "QUOTE_QUANTITY_INVALID"),
        (
            corrupted(quote(), amount=Decimal("-1")),
            "600000.SH",
            "QUOTE_QUANTITY_INVALID",
        ),
        (
            quote(quote_time=NOW + timedelta(seconds=1)),
            "600000.SH",
            "QUOTE_TIME_FUTURE",
        ),
        (quote(quote_time=NOW - timedelta(seconds=181)), "600000.SH", "QUOTE_STALE"),
        (
            corrupted(quote(), quote_time=datetime(2026, 7, 15, 2, 0)),
            "600000.SH",
            "QUOTE_TIME_INVALID",
        ),
    ],
)
def test_invalid_quote_returns_stable_reason(
    candidate: RealtimeQuote, symbol: str, code: str
) -> None:
    result = validate_quote(candidate, symbol=symbol, now=NOW)
    assert result.valid is False
    assert result.error_code == code


def test_validation_requires_timezone_aware_server_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        validate_quote(quote(), symbol="600000.SH", now=NOW.replace(tzinfo=None))


def test_quote_conflicts_only_when_difference_strictly_exceeds_threshold() -> None:
    assert compare_quotes(quote("10.00"), quote("10.02")).conflict is False
    comparison = compare_quotes(quote("10.00"), quote("10.03"))
    assert comparison.conflict is True
    assert comparison.threshold == Decimal("0.02006")


def test_quote_conflict_uses_relative_threshold_for_high_prices() -> None:
    comparison = compare_quotes(quote("100.00"), quote("100.20"))
    assert comparison.threshold == Decimal("0.20040")
    assert comparison.conflict is False


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"price": Decimal("NaN")}, "QUOTE_PRICE_INVALID"),
        ({"high": Decimal("Infinity")}, "QUOTE_OHLC_INVALID"),
        ({"price": Decimal("1e14")}, "QUOTE_PRICE_INVALID"),
        ({"amount": Decimal("1e20")}, "QUOTE_QUANTITY_INVALID"),
        ({"volume": 2_147_483_648}, "QUOTE_QUANTITY_INVALID"),
    ],
)
def test_quote_rejects_non_finite_and_database_oversized_values(
    changes: dict[str, object], code: str
) -> None:
    result = validate_quote(corrupted(quote(), **changes), symbol="600000.SH", now=NOW)
    assert result == QuoteValidation(False, code)


def test_quote_accepts_database_size_boundaries() -> None:
    candidate = corrupted(
        quote(),
        price=Decimal("99999999999999.999999"),
        high=Decimal("99999999999999.999999"),
        amount=Decimal("99999999999999999999.9999"),
        volume=2_147_483_647,
    )
    assert validate_quote(candidate, symbol="600000.SH", now=NOW).valid is True
