import asyncio
from types import SimpleNamespace
from uuid import uuid4

from long_invest.modules.strategies import jobs
from long_invest.modules.strategies.jobs import StrategyValidationOutcome
from long_invest.platform.jobs.contracts import JobExecutionContext


def context(config):
    return JobExecutionContext(
        job_id=uuid4(),
        fence_token=uuid4(),
        config=config,
    )


class Application:
    def __init__(self):
        self.validation_calls = []
        self.publish_calls = []

    async def record_validation_result_from_worker(self, run_id, **kwargs):
        self.validation_calls.append((run_id, kwargs))

    async def execute_publish(self, run_id):
        self.publish_calls.append(run_id)
        return SimpleNamespace(id=uuid4(), status="PUBLISHED")


class ValidationExecutor:
    async def execute(self, _run_id):
        return StrategyValidationOutcome(
            succeeded=True,
            evidence_snapshot={"checks": "verified"},
        )


def test_validation_job_fails_closed_until_executor_is_configured(monkeypatch):
    application = Application()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(jobs, "_validation_executor_factory", None)

    result = asyncio.run(
        jobs.strategy_validate(context({"validation_run_id": str(uuid4())}))
    )

    assert not result.success
    assert result.code == "STRATEGY_VALIDATION_EXECUTOR_UNAVAILABLE"
    assert application.validation_calls[0][1]["succeeded"] is False


def test_validation_job_records_executor_result(monkeypatch):
    application = Application()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(
        jobs,
        "_validation_executor_factory",
        lambda: ValidationExecutor(),
    )
    run_id = uuid4()

    result = asyncio.run(
        jobs.strategy_validate(context({"validation_run_id": str(run_id)}))
    )

    assert result.success
    assert application.validation_calls[0][0] == run_id
    assert application.validation_calls[0][1]["succeeded"] is True


def test_publish_job_executes_persistent_strategy_run(monkeypatch):
    application = Application()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    run_id = uuid4()

    result = asyncio.run(
        jobs.strategy_publish(context({"strategy_run_id": str(run_id)}))
    )

    assert result.success
    assert application.publish_calls == [run_id]
    assert result.data["strategy_run_id"] == str(run_id)


def test_strategy_jobs_reject_invalid_frozen_config():
    validation = asyncio.run(jobs.strategy_validate(context({})))
    publication = asyncio.run(jobs.strategy_publish(context({})))

    assert validation.code == "STRATEGY_VALIDATION_CONFIG_INVALID"
    assert publication.code == "STRATEGY_PUBLISH_CONFIG_INVALID"
