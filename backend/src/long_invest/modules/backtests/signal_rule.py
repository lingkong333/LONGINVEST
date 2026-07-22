from long_invest.modules.backtests.contracts import (
    BacktestSignalRuleInput,
    BacktestSignalRuleResult,
)
from long_invest.modules.signals.contracts import PriceZoneRuleInput, PriceZoneRulePort


class BacktestProductionSignalRule:
    rule_version = "signals-price-zone-v1"

    def __init__(self, production_rule: PriceZoneRulePort) -> None:
        self._production_rule = production_rule

    def evaluate(self, signal: BacktestSignalRuleInput) -> BacktestSignalRuleResult:
        result = self._production_rule.evaluate(
            PriceZoneRuleInput(
                price=signal.close_price,
                targets=signal.targets,
                previous_zone=signal.previous_zone,
                hysteresis_ratio=signal.hysteresis_ratio,
                hysteresis_min=signal.minimum_hysteresis,
            )
        )
        return BacktestSignalRuleResult(zone=result.zone)
