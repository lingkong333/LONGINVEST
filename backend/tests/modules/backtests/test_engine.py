from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from long_invest.modules.backtests.contracts import (
    BacktestPositionStatus,
    BacktestSignalRuleResult,
)
from long_invest.modules.backtests.engine import (
    BacktestBar,
    FixedTargetBacktestEngine,
    _annualized_return,
)
from long_invest.modules.market_data.contracts import AdjustmentTimelineEntry
from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.targets.contracts import TargetValues


class ProductionRuleFake:
    def evaluate(self, signal):
        price = signal.close_price
        values = signal.targets
        if price <= values.low_strong:
            zone = SignalZone.STRONG_LOW
        elif price <= values.low_watch:
            zone = SignalZone.LOW
        elif price >= values.high_strong:
            zone = SignalZone.STRONG_HIGH
        elif price >= values.high_watch:
            zone = SignalZone.HIGH
        else:
            zone = SignalZone.NORMAL
        return BacktestSignalRuleResult(zone=zone)


def _bar(day: int, open_: str, close: str) -> BacktestBar:
    return BacktestBar(
        trade_date=date(2025, 1, day),
        open_price=Decimal(open_),
        close_price=Decimal(close),
    )


def _targets() -> TargetValues:
    return TargetValues(
        low_strong=Decimal("8"),
        low_watch=Decimal("9"),
        high_watch=Decimal("12"),
        high_strong=Decimal("13"),
    )


def test_engine_executes_on_next_available_open_and_supports_multiple_rounds():
    item_id = uuid4()
    engine = FixedTargetBacktestEngine(ProductionRuleFake(), rule_version="rules-1")

    result = engine.run(
        item_id=item_id,
        security_id=uuid4(),
        bars=(
            _bar(2, "10", "8.5"),
            _bar(6, "8", "12.5"),
            _bar(7, "13", "8.5"),
            _bar(8, "8", "12.5"),
            _bar(9, "13", "10"),
        ),
        targets=_targets(),
        adjustments=(),
        initial_capital=Decimal("100000"),
        hysteresis_ratio=Decimal("0.02"),
        minimum_hysteresis=Decimal("0.02"),
    )

    actual_orders = [
        (item.direction.value, item.signal_date, item.execute_date)
        for item in result.orders
    ]
    assert actual_orders == [
        ("BUY", date(2025, 1, 2), date(2025, 1, 6)),
        ("SELL", date(2025, 1, 6), date(2025, 1, 7)),
        ("BUY", date(2025, 1, 7), date(2025, 1, 8)),
        ("SELL", date(2025, 1, 8), date(2025, 1, 9)),
    ]
    assert result.metric.completed_round_trips == 2
    assert result.metric.ending_equity == Decimal("264062.50")
    assert result.daily_results[-1].position_status is BacktestPositionStatus.FLAT


def test_engine_does_not_force_liquidate_and_marks_last_order_unfilled():
    result = FixedTargetBacktestEngine(
        ProductionRuleFake(), rule_version="rules-1"
    ).run(
        item_id=uuid4(),
        security_id=uuid4(),
        bars=(_bar(2, "10", "8.5"), _bar(3, "8", "12.5")),
        targets=_targets(),
        adjustments=(),
        initial_capital=Decimal("100000"),
        hysteresis_ratio=Decimal("0.02"),
        minimum_hysteresis=Decimal("0.02"),
    )

    assert result.orders[-1].status.value == "UNFILLED_AT_END"
    assert result.orders[-1].quantity is None
    assert result.metric.open_position_at_end is True
    assert result.metric.unfilled_order_count == 1
    assert result.metric.ending_equity == Decimal("156250.00")


def test_company_action_adjusts_targets_without_reforecasting():
    adjustment = AdjustmentTimelineEntry(
        event_date=date(2025, 1, 3),
        effective_date=date(2025, 1, 3),
        published_at=datetime(2025, 1, 1, tzinfo=UTC),
        source="eastmoney",
        adjustment_factor=Decimal("0.5"),
        data_hash="a" * 64,
    )

    result = FixedTargetBacktestEngine(
        ProductionRuleFake(), rule_version="rules-1"
    ).run(
        item_id=uuid4(),
        security_id=uuid4(),
        bars=(_bar(2, "10", "8.5"), _bar(3, "4", "6.5"), _bar(4, "7", "7")),
        targets=_targets(),
        adjustments=(adjustment,),
        initial_capital=Decimal("100000"),
        hysteresis_ratio=Decimal("0.02"),
        minimum_hysteresis=Decimal("0.02"),
    )

    assert len(result.adjustments) == 1
    assert result.adjustments[0].before_values.low_strong == Decimal("8.00")
    assert result.adjustments[0].after_values.low_strong == Decimal("4.00")
    assert result.orders[1].target_values.high_watch == Decimal("6.00")


def test_same_snapshot_produces_identical_business_results():
    item_id = uuid4()
    values = dict(
        item_id=item_id,
        security_id=uuid4(),
        bars=(_bar(2, "10", "8.5"), _bar(3, "8", "12.5"), _bar(4, "13", "10")),
        targets=_targets(),
        adjustments=(),
        initial_capital=Decimal("100000"),
        hysteresis_ratio=Decimal("0.02"),
        minimum_hysteresis=Decimal("0.02"),
    )
    engine = FixedTargetBacktestEngine(ProductionRuleFake(), rule_version="rules-1")

    first = engine.run(**values)
    second = engine.run(**values)

    assert first == second


def test_annualized_return_uses_compounding() -> None:
    assert _annualized_return(
        Decimal("110"), Decimal("100"), trading_days=126
    ) == Decimal("0.21000000")
