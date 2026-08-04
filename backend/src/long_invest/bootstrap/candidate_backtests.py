from __future__ import annotations

from long_invest.bootstrap.strategy_data import QfqStrategyDataPort
from long_invest.bootstrap.strategy_screening import (
    build_strategy_screening_application,
)
from long_invest.modules.backtests.candidate_application import (
    CandidateBacktestApplication,
)
from long_invest.modules.backtests.engine import FixedTargetBacktestEngine
from long_invest.modules.backtests.signal_rule import BacktestProductionSignalRule
from long_invest.modules.qfq.application import get_qfq_application
from long_invest.modules.signals.rules import ProductionPriceZoneRule
from long_invest.modules.strategies.application import get_strategy_application
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import get_database


def build_candidate_backtest_application() -> CandidateBacktestApplication:
    settings = get_settings()
    rule = BacktestProductionSignalRule(ProductionPriceZoneRule())
    return CandidateBacktestApplication(
        get_database(),
        candidates=build_strategy_screening_application(),
        strategies=get_strategy_application(),
        data=QfqStrategyDataPort(get_qfq_application()),
        engine=FixedTargetBacktestEngine(rule, rule_version=rule.rule_version),
        environment_version=settings.strategy_environment_version,
        runner_image_digest=settings.strategy_runner_image_digest,
    )


async def candidate_backtest_batch(context):
    return await build_candidate_backtest_application().run(context)
