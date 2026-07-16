from datetime import date
from decimal import Decimal

import pytest

from long_invest.modules.daily_data.quality import (
    DailyQualityContext,
    validate_daily_bar,
)


def _bar(**overrides):
    values = {
        "symbol": "600000.SH",
        "trading_date": date(2026, 7, 15),
        "open": Decimal("10.00"),
        "high": Decimal("10.50"),
        "low": Decimal("9.90"),
        "close": Decimal("10.20"),
        "volume": 100,
        "amount": Decimal("1020.00"),
        "source": "EASTMONEY",
    }
    values.update(overrides)
    return values


def test_valid_bar_passes_without_review() -> None:
    result = validate_daily_bar(
        _bar(),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(previous_close=Decimal("10.00")),
    )
    assert result.valid is True
    assert result.review_required is False
    assert result.code == "OK"


def test_volume_above_signed_32_bit_range_remains_valid() -> None:
    result = validate_daily_bar(
        _bar(volume=2_147_483_648),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(),
    )

    assert result.valid is True
    assert result.review_required is False


def test_bar_rejects_wrong_trading_date() -> None:
    result = validate_daily_bar(
        _bar(trading_date=date(2026, 7, 14)),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(),
    )
    assert result.code == "DAILY_BAR_DATE_MISMATCH"
    assert result.valid is False


def test_bar_rejects_wrong_symbol() -> None:
    result = validate_daily_bar(
        _bar(symbol="000001.SZ"),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(),
    )
    assert result.code == "DAILY_BAR_SYMBOL_MISMATCH"
    assert result.valid is False


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"open": Decimal("0")}, "DAILY_BAR_INVALID"),
        ({"high": Decimal("9.00")}, "DAILY_BAR_INVALID"),
        ({"low": Decimal("10.30")}, "DAILY_BAR_INVALID"),
        ({"volume": -1}, "DAILY_BAR_INVALID"),
        ({"amount": Decimal("-1")}, "DAILY_BAR_INVALID"),
    ],
)
def test_bar_rejects_invalid_ohlc_and_quantities(overrides, code) -> None:
    result = validate_daily_bar(
        _bar(**overrides),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(),
    )
    assert result.valid is False
    assert result.code == code


def test_duplicate_symbol_and_date_is_rejected() -> None:
    result = validate_daily_bar(
        _bar(),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(),
        seen_keys={("600000.SH", date(2026, 7, 15))},
    )
    assert result.code == "DAILY_BAR_DUPLICATE"
    assert result.valid is False


def test_previous_close_jump_requires_review_without_dropping_bar() -> None:
    result = validate_daily_bar(
        _bar(
            open=Decimal("20"),
            high=Decimal("21"),
            low=Decimal("19"),
            close=Decimal("20"),
        ),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(previous_close=Decimal("10")),
    )
    assert result.valid is True
    assert result.review_required is True
    assert result.code == "DAILY_BAR_PREVIOUS_CLOSE_ANOMALY"


@pytest.mark.parametrize(
    "context",
    [
        DailyQualityContext(is_new_listing=True, previous_close=Decimal("10")),
        DailyQualityContext(is_st=True, previous_close=Decimal("10")),
        DailyQualityContext(
            has_known_corporate_action=True, previous_close=Decimal("10")
        ),
    ],
)
def test_known_context_explains_jump_without_changing_prices(context) -> None:
    bar = _bar(
        open=Decimal("20"), high=Decimal("21"), low=Decimal("19"), close=Decimal("20")
    )
    result = validate_daily_bar(
        bar,
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=context,
    )
    assert result.valid is True
    assert result.review_required is False
    assert result.code == "DAILY_BAR_PREVIOUS_CLOSE_EXPLAINED"
    assert bar["close"] == Decimal("20")


def test_missing_or_unknown_schema_field_is_rejected() -> None:
    missing = _bar()
    del missing["close"]
    result = validate_daily_bar(
        missing,
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(),
    )
    assert result.code == "DAILY_BAR_SCHEMA_INVALID"

    result = validate_daily_bar(
        _bar(unexpected="field"),
        expected_symbol="600000.SH",
        expected_date=date(2026, 7, 15),
        context=DailyQualityContext(),
    )
    assert result.code == "DAILY_BAR_SCHEMA_INVALID"
