from typing import Any

from long_invest.bootstrap.jobs import security_master_refresh
from long_invest.platform.jobs.worker import HANDLERS
from long_invest.platform.jobs.worker import execute_job as execute_platform_job

HANDLERS["SECURITY_MASTER_REFRESH"] = security_master_refresh


def execute_job(job_id: str, outbox_id: str) -> dict[str, Any]:
    return execute_platform_job(job_id, outbox_id)
