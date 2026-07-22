from typing import Any

from long_invest.bootstrap.jobs import (
    daily_data_coordinate,
    daily_data_finalize,
    daily_data_item,
    daily_data_retry,
    qfq_refresh,
    quote_diagnostic,
    realtime_quote_cycle,
    security_master_refresh,
    signal_evaluate_batch,
    signal_reevaluate,
)
from long_invest.bootstrap.stage4_runtime import (
    build_backtest_application,
    build_target_application,
)
from long_invest.modules.backtests.application import build_backtest_job_handler
from long_invest.modules.strategies.jobs import strategy_publish, strategy_validate
from long_invest.modules.targets.jobs import (
    configure_target_job_application,
    target_calculate,
)
from long_invest.platform.jobs.worker import HANDLERS
from long_invest.platform.jobs.worker import execute_job as execute_platform_job

HANDLERS["SECURITY_MASTER_REFRESH"] = security_master_refresh
HANDLERS["REALTIME_QUOTE_CYCLE"] = realtime_quote_cycle
HANDLERS["QUOTE_DIAGNOSTIC"] = quote_diagnostic
HANDLERS["DAILY_DATA_COORDINATE"] = daily_data_coordinate
HANDLERS["DAILY_DATA_ITEM"] = daily_data_item
HANDLERS["DAILY_DATA_FINALIZE"] = daily_data_finalize
HANDLERS["DAILY_DATA_RETRY"] = daily_data_retry
HANDLERS["QFQ_REFRESH"] = qfq_refresh
HANDLERS["SIGNAL_EVALUATE_BATCH"] = signal_evaluate_batch
HANDLERS["SIGNAL_REEVALUATE"] = signal_reevaluate
HANDLERS["STRATEGY_VALIDATE"] = strategy_validate
HANDLERS["STRATEGY_PUBLISH"] = strategy_publish
configure_target_job_application(build_target_application)
HANDLERS["TARGET_CALCULATE"] = target_calculate
HANDLERS["BACKTEST_SINGLE"] = build_backtest_job_handler(
    build_backtest_application()
)


def execute_job(job_id: str, outbox_id: str) -> dict[str, Any]:
    return execute_platform_job(job_id, outbox_id)
