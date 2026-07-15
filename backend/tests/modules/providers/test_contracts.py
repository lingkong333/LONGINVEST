from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyBarRequest,
    ProviderCapability,
    ProviderCode,
    RealtimeQuote,
    SecurityMasterRecord,
)


def test_provider_contract_enumerations_are_stable() -> None:
    assert {item.value for item in ProviderCapability} == {
        "SECURITY_MASTER",
        "REALTIME_QUOTE_BATCH",
        "DAILY_BAR_UNADJUSTED",
        "HISTORICAL_DAILY_UNADJUSTED",
        "HISTORICAL_DAILY_QFQ",
    }
    assert {item.value for item in ProviderCode} == {"EASTMONEY", "SINA"}


def test_contracts_accept_valid_internal_symbols_and_decimal_values() -> None:
    now = datetime.now(UTC)
    master = SecurityMasterRecord(
        symbol="600000.SH",
        name="浦发银行",
        market="SH",
        security_type="STOCK",
        listed_on=date(1999, 11, 10),
        delisted_on=None,
        listed=True,
        is_st=False,
        suspended=False,
        source=ProviderCode.EASTMONEY,
        observed_at=now,
    )
    quote = RealtimeQuote(
        symbol="000001.SZ",
        price=Decimal("10.01"),
        open=Decimal("9.90"),
        high=Decimal("10.10"),
        low=Decimal("9.80"),
        previous_close=Decimal("9.88"),
        volume=100,
        amount=Decimal("1001"),
        quote_time=now,
        received_at=now,
        source=ProviderCode.EASTMONEY,
    )
    request = DailyBarRequest(
        symbol="430047.BJ",
        start=date(2025, 1, 1),
        end=date(2025, 1, 2),
        capability=ProviderCapability.HISTORICAL_DAILY_QFQ,
    )
    assert master.symbol == "600000.SH"
    assert quote.price == Decimal("10.01")
    assert request.symbol == "430047.BJ"


@pytest.mark.parametrize(
    "symbol", ["600000", "600000.SZ", "000001.SH", "630000.BJ", "ABC.SH"]
)
def test_internal_symbol_rejects_invalid_market_ranges(symbol: str) -> None:
    with pytest.raises(ValueError):
        DailyBarRequest(
            symbol=symbol,
            start=date(2025, 1, 1),
            end=date(2025, 1, 2),
            capability=ProviderCapability.DAILY_BAR_UNADJUSTED,
        )


def test_quote_requires_timezone_and_valid_ohlc() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        RealtimeQuote(
            symbol="600000.SH",
            price=Decimal("11"),
            open=Decimal("10"),
            high=Decimal("10.5"),
            low=Decimal("9"),
            previous_close=Decimal("10"),
            volume=1,
            amount=Decimal("1"),
            quote_time=now.replace(tzinfo=None),
            received_at=now,
            source=ProviderCode.EASTMONEY,
        )


def test_daily_bar_rejects_negative_quantity_and_invalid_ohlc() -> None:
    with pytest.raises(ValueError):
        DailyBar(
            symbol="600000.SH",
            trading_date=date(2025, 1, 2),
            open=Decimal("10"),
            high=Decimal("9"),
            low=Decimal("8"),
            close=Decimal("9"),
            volume=-1,
            amount=Decimal("1"),
            source=ProviderCode.EASTMONEY,
            capability=ProviderCapability.DAILY_BAR_UNADJUSTED,
        )


@pytest.mark.parametrize("zero_field", ["open", "high", "low", "close"])
def test_daily_bar_requires_strictly_positive_ohlc(zero_field: str) -> None:
    values = {
        "open": Decimal("10"),
        "high": Decimal("10"),
        "low": Decimal("10"),
        "close": Decimal("10"),
    }
    values[zero_field] = Decimal("0")
    with pytest.raises(ValueError, match="positive"):
        DailyBar(
            symbol="600000.SH",
            trading_date=date(2025, 1, 2),
            volume=1,
            amount=Decimal("1"),
            source=ProviderCode.EASTMONEY,
            capability=ProviderCapability.DAILY_BAR_UNADJUSTED,
            **values,
        )
