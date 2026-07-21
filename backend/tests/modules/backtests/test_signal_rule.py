from datetime import date
from decimal import Decimal
from uuid import uuid4

from long_invest.modules.backtests.contracts import (
    BacktestPositionStatus,
    BacktestSignalRuleInput,
)
from long_invest.modules.backtests.signal_rule import BacktestProductionSignalRule
from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.signals.rules import ProductionPriceZoneRule
from long_invest.modules.targets.contracts import TargetValues


def test_backtest_rule_maps_to_the_public_production_rule() -> None:
    rule = BacktestProductionSignalRule(ProductionPriceZoneRule())

    result = rule.evaluate(
        BacktestSignalRuleInput(
            security_id=uuid4(),
            trade_date=date(2025, 1, 2),
            close_price=Decimal("8"),
            targets=TargetValues(
                low_strong="8", low_watch="9", high_watch="11", high_strong="12"
            ),
            previous_zone=SignalZone.NORMAL,
            position_status=BacktestPositionStatus.FLAT,
            hysteresis_ratio=Decimal("0.02"),
            minimum_hysteresis=Decimal("0.02"),
        )
    )

    assert result.zone is SignalZone.STRONG_LOW
