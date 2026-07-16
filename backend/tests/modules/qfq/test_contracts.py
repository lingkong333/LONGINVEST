from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.modules.qfq.contracts import (
    Page,
    QfqBarInput,
    QfqBarView,
    QfqDatasetLifecycle,
    QfqDatasetView,
    QfqFreshness,
    QfqRefreshStatus,
    QfqRefreshView,
    QfqValidationError,
    RefreshQfq,
)


def make_command(**overrides: object) -> RefreshQfq:
    values = {
        "security_id": uuid4(),
        "symbol": "600000.SH",
        "start": date(2026, 7, 14),
        "end": date(2026, 7, 16),
        "as_of_date": date(2026, 7, 16),
        "expected_trade_dates": (
            date(2026, 7, 14),
            date(2026, 7, 15),
            date(2026, 7, 16),
        ),
        "input_daily_version": 3,
        "trigger_reason": "MANUAL",
        "request_id": "req-qfq-1",
        "idempotency_key": "qfq-1",
        "actor_user_id": str(uuid4()),
    }
    values.update(overrides)
    return RefreshQfq(**values)  # type: ignore[arg-type]


def make_bar(**overrides: object) -> QfqBarInput:
    values = {
        "trade_date": date(2026, 7, 16),
        "open": Decimal("10.00"),
        "high": Decimal("10.80"),
        "low": Decimal("9.80"),
        "close": Decimal("10.50"),
        "volume": 100,
        "amount": Decimal("1030.00"),
    }
    values.update(overrides)
    return QfqBarInput(**values)  # type: ignore[arg-type]


def test_exact_state_values_are_stable() -> None:
    assert [item.value for item in QfqDatasetLifecycle] == [
        "STAGING",
        "CURRENT",
        "SUPERSEDED",
    ]
    assert [item.value for item in QfqFreshness] == ["FRESH", "STALE"]
    assert [item.value for item in QfqRefreshStatus] == [
        "PENDING",
        "FETCHING",
        "VALIDATING",
        "COMMITTING",
        "SUCCEEDED",
        "FAILED",
        "TIMED_OUT",
        "SUPERSEDED",
    ]


def test_refresh_command_is_frozen_and_accepts_a_bounded_window() -> None:
    command = make_command()

    assert command.start <= command.as_of_date == command.end
    assert command.expected_trade_dates[-1] == command.end
    assert command.input_daily_version == 3
    with pytest.raises(FrozenInstanceError):
        command.symbol = "000001.SZ"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"start": date(2026, 7, 17)}, "window"),
        ({"as_of_date": date(2026, 7, 15)}, "window"),
        ({"input_daily_version": 0}, "version"),
        ({"symbol": "600000"}, "symbol"),
        ({"trigger_reason": "  "}, "trigger_reason"),
        ({"request_id": ""}, "request_id"),
        ({"idempotency_key": ""}, "idempotency_key"),
        ({"actor_user_id": ""}, "actor_user_id"),
    ],
)
def test_refresh_command_rejects_invalid_values(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_command(**overrides)


@pytest.mark.parametrize(
    "expected_trade_dates",
    [
        (),
        (date(2026, 7, 14), date(2026, 7, 14), date(2026, 7, 16)),
        (date(2026, 7, 15), date(2026, 7, 14), date(2026, 7, 16)),
        (date(2026, 7, 13), date(2026, 7, 16)),
        (date(2026, 7, 14), date(2026, 7, 15)),
    ],
)
def test_refresh_command_rejects_invalid_expected_trade_dates(
    expected_trade_dates: tuple[date, ...],
) -> None:
    with pytest.raises(ValueError, match="expected_trade_dates"):
        make_command(expected_trade_dates=expected_trade_dates)


def test_refresh_command_copies_expected_trade_dates() -> None:
    dates = [date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)]

    command = make_command(expected_trade_dates=dates)
    dates.clear()

    assert command.expected_trade_dates == (
        date(2026, 7, 14),
        date(2026, 7, 15),
        date(2026, 7, 16),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"open": Decimal("0")},
        {"close": Decimal("NaN")},
        {"high": Decimal("9.99")},
        {"low": Decimal("10.60")},
        {"volume": -1},
        {"amount": Decimal("-0.01")},
    ],
)
def test_bar_rejects_invalid_ohlc_or_quantities(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        make_bar(**overrides)


def test_bar_accepts_storage_boundaries_and_trailing_zeroes() -> None:
    bar = QfqBarInput(
        trade_date=date(2026, 7, 16),
        open=Decimal("999999999999.9999990"),
        high=Decimal("999999999999.9999990"),
        low=Decimal("0.0000010"),
        close=Decimal("0.0000010"),
        volume=9223372036854775807,
        amount=Decimal("99999999999999999999.99990"),
    )

    assert bar.volume == 9223372036854775807


@pytest.mark.parametrize(
    "price",
    [
        Decimal("1e1000000"),
        Decimal("1000000000000"),
        Decimal("0.0000001"),
        Decimal("999999999999.9999991"),
    ],
)
def test_bar_rejects_price_outside_numeric_18_6(price: Decimal) -> None:
    with pytest.raises(ValueError, match="price storage limit"):
        QfqBarInput(
            trade_date=date(2026, 7, 16),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0,
            amount=Decimal("0"),
        )


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("1e1000000"),
        Decimal("100000000000000000000"),
        Decimal("0.00001"),
        Decimal("99999999999999999999.99991"),
    ],
)
def test_bar_rejects_amount_outside_numeric_24_4(amount: Decimal) -> None:
    with pytest.raises(ValueError, match="amount storage limit"):
        make_bar(amount=amount)


def test_bar_rejects_volume_above_bigint() -> None:
    with pytest.raises(ValueError, match="volume"):
        make_bar(volume=9223372036854775808)


def test_bar_is_frozen() -> None:
    bar = make_bar()

    with pytest.raises(FrozenInstanceError):
        bar.close = Decimal("11")  # type: ignore[misc]


def test_validation_error_exposes_stable_code() -> None:
    error = QfqValidationError("QFQ_EMPTY_RESULT")

    assert error.code == "QFQ_EMPTY_RESULT"
    assert str(error) == "QFQ_EMPTY_RESULT"


def test_dataset_view_and_page_are_read_only() -> None:
    assert QfqRefreshView.__dataclass_params__.frozen is True
    now = datetime(2026, 7, 16, tzinfo=UTC)
    dataset = QfqDatasetView(
        id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        version=1,
        requested_start=date(2026, 7, 14),
        requested_end=date(2026, 7, 16),
        actual_start=date(2026, 7, 14),
        actual_end=date(2026, 7, 16),
        as_of_date=date(2026, 7, 16),
        provider="EASTMONEY",
        provider_contract_version="1",
        anchor_date=date(2026, 7, 16),
        anchor_close="10.5",
        row_count=3,
        checksum="a" * 64,
        lifecycle=QfqDatasetLifecycle.CURRENT,
        freshness=QfqFreshness.FRESH,
        stale_reason=None,
        created_at=now,
        activated_at=now,
        superseded_at=None,
    )
    rows = [
        QfqBarView(
            trade_date=date(2026, 7, 16),
            open="10",
            high="11",
            low="9",
            close="10.5",
            volume=100,
            amount="1000",
        )
    ]
    page = Page(items=rows, total=1, page=1, page_size=50)

    rows.clear()
    assert len(page.items) == 1
    with pytest.raises(FrozenInstanceError):
        dataset.version = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="pagination"):
        Page(items=(), total=-1)
