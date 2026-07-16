from typing import Any

from long_invest.bootstrap.jobs import (
    daily_data_coordinate,
    daily_data_finalize,
    daily_data_item,
    daily_data_retry,
    quote_diagnostic,
    realtime_quote_cycle,
    security_master_refresh,
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


def execute_job(job_id: str, outbox_id: str) -> dict[str, Any]:
    return execute_platform_job(job_id, outbox_id)
