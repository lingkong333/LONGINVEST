from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.backtests.contracts import (
    BacktestDailyResultView,
    BacktestDateRange,
    BacktestErrorCode,
    BacktestItemStatus,
    BacktestSignalInput,
    BacktestTaskSnapshot,
)
from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.targets.contracts import TargetValues


def test_backtest_date_range_requires_non_overlapping_training_and_test_dates() -> None:
    date_range = BacktestDateRange(
        training_start_date=date(2024, 1, 1),
        training_end_date=date(2024, 12, 31),
        test_start_date=date(2025, 1, 1),
        test_end_date=date(2025, 12, 31),
    )
    assert date_range.test_start_date == date(2025, 1, 1)
    with pytest.raises(ValidationError):
        BacktestDateRange(
            training_start_date=date(2024, 1, 1),
            training_end_date=date(2024, 12, 31),
            test_start_date=date(2024, 12, 31),
            test_end_date=date(2025, 12, 31),
        )


def test_backtest_signal_input_is_strict_and_uses_decimal_targets() -> None:
    signal = BacktestSignalInput(
        security_id=uuid4(),
        trade_date=date(2025, 1, 2),
        close_price=Decimal("10.00"),
        targets=TargetValues(
            low_strong="8", low_watch="9", high_watch="11", high_strong="12"
        ),
    )
    assert signal.close_price == Decimal("10.00")
    with pytest.raises(ValidationError):
        BacktestSignalInput.model_validate(signal.model_dump() | {"extra": True})


def test_backtest_status_and_error_codes_are_stable() -> None:
    assert BacktestItemStatus.FROZEN.value == "FROZEN"
    assert BacktestErrorCode.ADJUSTMENT_DATA_UNAVAILABLE.value == (
        "ADJUSTMENT_DATA_UNAVAILABLE"
    )


def test_backtest_contracts_expose_frozen_replay_snapshots() -> None:
    snapshot = BacktestTaskSnapshot(
        id=uuid4(),
        date_range=BacktestDateRange(
            training_start_date=date(2024, 1, 1),
            training_end_date=date(2024, 12, 31),
            test_start_date=date(2025, 1, 1),
            test_end_date=date(2025, 12, 31),
        ),
        source_code_hash="a" * 64,
        parameter_hash="b" * 64,
        environment_version="runner-1",
        rule_version="rules-1",
        initial_capital=Decimal("100000"),
        price_basis="UNADJUSTED",
        data_source="provider",
    )
    assert snapshot.price_basis == "UNADJUSTED"
    assert BacktestDailyResultView.__name__ == "BacktestDailyResultView"
    assert SignalZone.UNKNOWN.value == "UNKNOWN"
