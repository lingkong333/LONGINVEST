from decimal import Decimal

from long_invest.modules.signals.contracts import PriceZoneRuleInput, SignalZone
from long_invest.modules.signals.rules import ProductionPriceZoneRule
from long_invest.modules.targets.contracts import TargetValues


def test_public_price_zone_rule_reuses_production_hysteresis() -> None:
    rule = ProductionPriceZoneRule()
    targets = TargetValues(
        low_strong="8", low_watch="9", high_watch="11", high_strong="12"
    )

    inside_buffer = rule.evaluate(
        PriceZoneRuleInput(
            price="9.10",
            targets=targets,
            previous_zone=SignalZone.LOW,
            hysteresis_ratio=Decimal("0.02"),
            hysteresis_min=Decimal("0.02"),
        )
    )
    beyond_buffer = rule.evaluate(
        PriceZoneRuleInput(
            price="9.19",
            targets=targets,
            previous_zone=SignalZone.LOW,
            hysteresis_ratio=Decimal("0.02"),
            hysteresis_min=Decimal("0.02"),
        )
    )

    assert inside_buffer.zone is SignalZone.LOW
    assert beyond_buffer.zone is SignalZone.NORMAL
