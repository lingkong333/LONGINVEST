from long_invest.modules.signals.contracts import (
    EvaluationReason,
    PriceZoneRuleInput,
    PriceZoneRuleResult,
)
from long_invest.modules.signals.state_machine import next_zone_for_values


class ProductionPriceZoneRule:
    def evaluate(self, value: PriceZoneRuleInput) -> PriceZoneRuleResult:
        return PriceZoneRuleResult(
            zone=next_zone_for_values(
                current=value.previous_zone,
                price=value.price,
                targets=value.targets,
                ratio=value.hysteresis_ratio,
                minimum=value.hysteresis_min,
                reason=EvaluationReason.SCHEDULED_QUOTE,
            )
        )
