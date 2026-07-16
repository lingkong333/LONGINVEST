from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from long_invest.modules.qfq.contracts import (
    QfqBarInput,
    QfqValidationError,
    RefreshQfq,
)
from long_invest.modules.qfq.validation import validate_qfq_window


def command() -> RefreshQfq:
    return RefreshQfq(
        security_id=uuid4(),
        symbol="600000.SH",
        start=date(2026, 7, 14),
        end=date(2026, 7, 16),
        as_of_date=date(2026, 7, 16),
        input_daily_version=3,
        trigger_reason="MANUAL",
        request_id="req-qfq-1",
        idempotency_key="qfq-1",
        actor_user_id=str(uuid4()),
    )


def bar(trade_date: date, close: str, *, scale: str = "1") -> QfqBarInput:
    factor = Decimal(scale)
    close_value = Decimal(close) * factor
    return QfqBarInput(
        trade_date=trade_date,
        open=close_value,
        high=close_value + Decimal("0.5") * factor,
        low=close_value - Decimal("0.5") * factor,
        close=close_value,
        volume=100,
        amount=Decimal("1000"),
    )


def valid_bars() -> tuple[QfqBarInput, ...]:
    return (
        bar(date(2026, 7, 14), "9.5"),
        bar(date(2026, 7, 15), "10"),
        bar(date(2026, 7, 16), "10.5"),
    )


def assert_error(code: str, bars: object, expected_close: str = "10.5") -> None:
    with pytest.raises(QfqValidationError) as captured:
        validate_qfq_window(command(), bars, Decimal(expected_close))  # type: ignore[arg-type]
    assert captured.value.code == code


def test_valid_window_returns_frozen_normalized_result() -> None:
    source = list(valid_bars())
    original = source.copy()

    result = validate_qfq_window(command(), source, Decimal("10.50"))

    assert source == original
    assert result.bars == tuple(original)
    assert result.anchor_date == date(2026, 7, 16)
    assert result.anchor_close == Decimal("10.5")
    assert result.row_count == 3
    assert len(result.checksum) == 64
    with pytest.raises(FrozenInstanceError):
        result.row_count = 4  # type: ignore[misc]


def test_empty_result_is_rejected() -> None:
    assert_error("QFQ_EMPTY_RESULT", ())


def test_descending_input_is_rejected_without_silent_sorting() -> None:
    bars = (valid_bars()[1], valid_bars()[0], valid_bars()[2])
    assert_error("QFQ_DATE_ORDER_INVALID", bars)


def test_duplicate_date_is_rejected() -> None:
    bars = (valid_bars()[0], valid_bars()[1], valid_bars()[1], valid_bars()[2])
    assert_error("QFQ_DUPLICATE_DATE", bars)


@pytest.mark.parametrize(
    "bars",
    [
        (bar(date(2026, 7, 13), "9"), *valid_bars()),
        (*valid_bars(), bar(date(2026, 7, 17), "11")),
        valid_bars()[:-1],
    ],
)
def test_out_of_window_or_missing_end_is_rejected(
    bars: tuple[QfqBarInput, ...],
) -> None:
    assert_error("QFQ_WINDOW_INCOMPLETE", bars, bars[-1].close.to_eng_string())


def test_adjustment_basis_mismatch_is_rejected() -> None:
    assert_error("QFQ_ADJUSTMENT_BASIS_MISMATCH", valid_bars(), "10.51")


def test_checksum_is_stable_for_equivalent_decimal_encodings() -> None:
    first = validate_qfq_window(command(), valid_bars(), Decimal("10.5000"))
    equivalent = tuple(
        QfqBarInput(
            trade_date=item.trade_date,
            open=Decimal(f"{item.open:.4f}"),
            high=Decimal(f"{item.high:.4f}"),
            low=Decimal(f"{item.low:.4f}"),
            close=Decimal(f"{item.close:.4f}"),
            volume=item.volume,
            amount=Decimal(f"{item.amount:.4f}"),
        )
        for item in valid_bars()
    )
    second = validate_qfq_window(command(), equivalent, Decimal("10.5"))

    assert first.checksum == second.checksum


def high_precision_bars(close: str) -> tuple[QfqBarInput, ...]:
    final_close = Decimal(close)
    return (
        bar(date(2026, 7, 14), "1.1"),
        bar(date(2026, 7, 15), "1.1"),
        QfqBarInput(
            trade_date=date(2026, 7, 16),
            open=final_close,
            high=Decimal("2"),
            low=Decimal("1"),
            close=final_close,
            volume=100,
            amount=Decimal("1000"),
        ),
    )


def test_checksum_preserves_adjacent_high_precision_values() -> None:
    first_close = "1.1234567890123456789012345678901"
    second_close = "1.1234567890123456789012345678902"

    first = validate_qfq_window(
        command(), high_precision_bars(first_close), Decimal(first_close)
    )
    second = validate_qfq_window(
        command(), high_precision_bars(second_close), Decimal(second_close)
    )

    assert first.checksum != second.checksum


def test_checksum_does_not_depend_on_decimal_context_precision() -> None:
    close = "1.1234567890123456789012345678901"
    with localcontext() as context:
        context.prec = 6
        low_precision = validate_qfq_window(
            command(), high_precision_bars(close), Decimal(close)
        )
    with localcontext() as context:
        context.prec = 50
        high_precision = validate_qfq_window(
            command(), high_precision_bars(close), Decimal(close)
        )

    assert low_precision.checksum == high_precision.checksum


def test_checksum_canonicalizes_negative_zero() -> None:
    positive_zero = tuple(replace(item, amount=Decimal("0")) for item in valid_bars())
    negative_zero = tuple(
        replace(item, amount=Decimal("-0.000")) for item in valid_bars()
    )

    first = validate_qfq_window(command(), positive_zero, Decimal("10.5"))
    second = validate_qfq_window(command(), negative_zero, Decimal("10.5"))

    assert first.checksum == second.checksum
