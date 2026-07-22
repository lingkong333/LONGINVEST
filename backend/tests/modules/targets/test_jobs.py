from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from long_invest.modules.targets import jobs
from long_invest.modules.targets.strategy_service import CalculationResult
from long_invest.platform.jobs.contracts import JobExecutionContext


def context(config):
    return JobExecutionContext(job_id=uuid4(), fence_token=uuid4(), config=config)


@pytest.mark.anyio
async def test_target_calculate_handler_executes_only_frozen_run_id(
    monkeypatch,
) -> None:
    run_id = uuid4()
    application = SimpleNamespace(
        execute=AsyncMock(
            return_value=CalculationResult(
                "TARGET_CALCULATION_SUCCEEDED", run_id, uuid4()
            )
        )
    )
    monkeypatch.setattr(jobs, "_application_factory", lambda: application)

    result = await jobs.target_calculate(context({"run_id": str(run_id)}))

    assert result.success is True
    assert result.data["run_id"] == str(run_id)
    application.execute.assert_awaited_once_with(run_id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "config",
    [
        {},
        {"run_id": "not-a-uuid"},
        {"run_id": str(uuid4()), "subscription_id": str(uuid4())},
    ],
)
async def test_target_calculate_handler_rejects_non_minimal_config(config) -> None:
    result = await jobs.target_calculate(context(config))

    assert result.success is False
    assert result.code == "TARGET_CALCULATE_CONFIG_INVALID"
    assert result.retryable is False


@pytest.mark.anyio
async def test_target_calculate_handler_reports_terminal_calculation_failure(
    monkeypatch,
) -> None:
    run_id = uuid4()
    application = SimpleNamespace(
        execute=AsyncMock(
            return_value=CalculationResult("TARGET_CALCULATION_FAILED", run_id)
        )
    )
    monkeypatch.setattr(jobs, "_application_factory", lambda: application)

    result = await jobs.target_calculate(context({"run_id": str(run_id)}))

    assert result.success is False
    assert result.code == "TARGET_CALCULATION_FAILED"
    assert result.retryable is False
