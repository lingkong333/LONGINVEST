from datetime import date

import pytest

from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityMasterItem,
    SecurityType,
    SymbolUniverseQuery,
    assess_monitoring_eligibility,
    validate_symbol,
)


@pytest.mark.parametrize("symbol", ["600000.SH", "000001.SZ", "430047.BJ"])
def test_internal_a_share_symbols_are_accepted(symbol: str) -> None:
    assert validate_symbol(symbol) == symbol


@pytest.mark.parametrize(
    "symbol",
    ["600000", "sh600000", "600000.sh", "00700.HK", "AAPL.US", "60000.SH"],
)
def test_noncanonical_symbols_are_rejected(symbol: str) -> None:
    with pytest.raises(ValueError, match="统一代码"):
        validate_symbol(symbol)


def test_symbol_universe_query_normalizes_duplicates_and_order() -> None:
    query = SymbolUniverseQuery(
        symbols=("600000.SH", "000001.SZ", "600000.SH")
    )

    assert query.symbols == ("000001.SZ", "600000.SH")


def test_symbol_universe_query_rejects_an_invalid_symbol() -> None:
    with pytest.raises(ValueError, match="统一代码"):
        SymbolUniverseQuery(symbols=("600000",))


def test_symbol_universe_query_rejects_more_than_200_symbols() -> None:
    symbols = tuple(f"{value:06d}.SZ" for value in range(201))

    with pytest.raises(ValueError, match="最多包含 200 只股票"):
        SymbolUniverseQuery(symbols=symbols)


def security_item(
    *,
    security_type: SecurityType = SecurityType.A_SHARE,
    listing_status: ListingStatus = ListingStatus.LISTED,
    suspended: bool = False,
) -> SecurityMasterItem:
    return SecurityMasterItem(
        symbol="600000.SH",
        exchange_code="600000",
        name="浦发银行",
        market=Market.SH,
        security_type=security_type,
        listing_status=listing_status,
        listed_on=date(1999, 11, 10),
        delisted_on=None,
        is_st=False,
        is_suspended=suspended,
        provider_codes={"eastmoney": "1.600000", "sina": "sh600000"},
    )


def test_suspended_a_share_remains_eligible_for_monitoring() -> None:
    eligibility = assess_monitoring_eligibility(security_item(suspended=True))

    assert eligibility.eligible is True
    assert eligibility.code == "ELIGIBLE"


@pytest.mark.parametrize(
    "security_type",
    [
        SecurityType.ETF,
        SecurityType.CONVERTIBLE_BOND,
        SecurityType.B_SHARE,
        SecurityType.FUND,
        SecurityType.INDEX,
        SecurityType.HK_STOCK,
        SecurityType.US_STOCK,
    ],
)
def test_non_a_share_types_have_a_stable_rejection(security_type: SecurityType) -> None:
    eligibility = assess_monitoring_eligibility(
        security_item(security_type=security_type)
    )

    assert eligibility.eligible is False
    assert eligibility.code == "SECURITY_TYPE_UNSUPPORTED"


def test_delisted_a_share_cannot_be_newly_monitored() -> None:
    eligibility = assess_monitoring_eligibility(
        security_item(listing_status=ListingStatus.DELISTED)
    )

    assert eligibility.eligible is False
    assert eligibility.code == "SECURITY_DELISTED"
