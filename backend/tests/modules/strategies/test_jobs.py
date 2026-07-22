import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

from long_invest.modules.strategies import jobs
from long_invest.modules.strategies.jobs import StrategyValidationOutcome
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import JobExecutionContext


def context(config):
    return JobExecutionContext(
        job_id=uuid4(),
        fence_token=uuid4(),
        config=config,
    )


def validation_config(application):
    return {
        "validation_run_id": str(application.validation_run.id),
        "backtest_task_id": str(uuid4()),
    }


class Application:
    def __init__(self, *, validation_status="PENDING", error_code=None):
        self.validation_calls = []
        self.publish_calls = []
        self.validation_run = SimpleNamespace(
            id=uuid4(), status=validation_status, error_code=error_code
        )
        self.raise_after_validation_commit = False

    async def get_validation_run(self, run_id):
        assert run_id == self.validation_run.id
        return self.validation_run

    async def record_validation_result_from_worker(self, run_id, **kwargs):
        self.validation_calls.append((run_id, kwargs))
        self.validation_run.status = "SUCCEEDED" if kwargs["succeeded"] else "FAILED"
        self.validation_run.error_code = kwargs["error_code"]
        if self.raise_after_validation_commit:
            self.raise_after_validation_commit = False
            raise OSError("database confirmation contains secret detail")
        return self.validation_run

    async def execute_publish(self, run_id):
        self.publish_calls.append(run_id)
        return SimpleNamespace(id=uuid4(), status="PUBLISHED")


class ValidationExecutor:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    async def execute(self, run_id, backtest_task_id):
        self.calls.append((run_id, backtest_task_id))
        if self.failure is not None:
            raise self.failure
        return StrategyValidationOutcome(
            succeeded=True,
            evidence_snapshot={"checks": "verified"},
        )


def test_validation_job_fails_closed_until_executor_is_configured(monkeypatch):
    application = Application()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(jobs, "_validation_executor_factory", None)

    result = asyncio.run(
        jobs.strategy_validate(context(validation_config(application)))
    )

    assert not result.success
    assert result.code == "STRATEGY_VALIDATION_EXECUTOR_UNAVAILABLE"
    assert application.validation_calls[0][1]["succeeded"] is False


def test_validation_job_records_executor_result(monkeypatch):
    application = Application()
    executor = ValidationExecutor()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(
        jobs,
        "_validation_executor_factory",
        lambda: executor,
    )
    run_id = application.validation_run.id
    config = validation_config(application)

    result = asyncio.run(jobs.strategy_validate(context(config)))

    assert result.success
    assert application.validation_calls[0][0] == run_id
    assert application.validation_calls[0][1]["succeeded"] is True
    assert executor.calls == [(run_id, UUID(config["backtest_task_id"]))]


def test_validation_job_replays_terminal_result_without_executor(monkeypatch):
    application = Application(validation_status="SUCCEEDED")
    executor = ValidationExecutor()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(jobs, "_validation_executor_factory", lambda: executor)

    result = asyncio.run(
        jobs.strategy_validate(
            context({"validation_run_id": str(application.validation_run.id)})
        )
    )

    assert result.success
    assert result.data["replayed"] is True
    assert executor.calls == []
    assert application.validation_calls == []


def test_validation_job_replays_terminal_failure_without_executor(monkeypatch):
    application = Application(
        validation_status="FAILED", error_code="STRATEGY_SAMPLE_FAILED"
    )
    executor = ValidationExecutor()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(jobs, "_validation_executor_factory", lambda: executor)
    config = validation_config(application)

    result = asyncio.run(jobs.strategy_validate(context(config)))

    assert not result.success
    assert result.code == "STRATEGY_SAMPLE_FAILED"
    assert executor.calls == []
    assert application.validation_calls == []


def test_validation_confirmation_loss_reuses_committed_result(monkeypatch):
    application = Application()
    application.raise_after_validation_commit = True
    executor = ValidationExecutor()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(jobs, "_validation_executor_factory", lambda: executor)
    config = validation_config(application)

    result = asyncio.run(jobs.strategy_validate(context(config)))

    assert result.success
    assert application.validation_run.status == "SUCCEEDED"
    assert executor.calls == [
        (application.validation_run.id, UUID(config["backtest_task_id"]))
    ]
    assert len(application.validation_calls) == 1


def test_pending_validation_without_backtest_id_fails_closed(monkeypatch):
    application = Application()
    executor = ValidationExecutor()
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(jobs, "_validation_executor_factory", lambda: executor)

    result = asyncio.run(
        jobs.strategy_validate(
            context({"validation_run_id": str(application.validation_run.id)})
        )
    )

    assert not result.success
    assert result.code == "STRATEGY_VALIDATION_CONFIG_INVALID"
    assert application.validation_run.status == "FAILED"
    assert executor.calls == []


def test_validation_executor_exception_is_sanitized_and_settled(monkeypatch):
    application = Application()
    executor = ValidationExecutor(RuntimeError("secret sandbox path"))
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)
    monkeypatch.setattr(jobs, "_validation_executor_factory", lambda: executor)

    result = asyncio.run(
        jobs.strategy_validate(context(validation_config(application)))
    )

    assert not result.success
    assert not result.retryable
    assert result.code == "STRATEGY_VALIDATION_EXECUTION_FAILED"
    assert "secret" not in result.message
    assert application.validation_run.status == "FAILED"


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


def test_publish_temporary_error_finishes_job_for_explicit_retry(monkeypatch):
    application = Application()

    async def fail(_run_id):
        raise AppError(
            code="STRATEGY_BACKEND_UNAVAILABLE",
            message="策略服务暂时不可用",
            status_code=503,
        )

    application.execute_publish = fail
    monkeypatch.setattr(jobs, "get_strategy_application", lambda: application)

    result = asyncio.run(
        jobs.strategy_publish(context({"strategy_run_id": str(uuid4())}))
    )

    assert not result.success
    assert not result.retryable


def test_strategy_jobs_reject_invalid_frozen_config():
    validation = asyncio.run(jobs.strategy_validate(context({})))
    publication = asyncio.run(jobs.strategy_publish(context({})))

    assert validation.code == "STRATEGY_VALIDATION_CONFIG_INVALID"
    assert publication.code == "STRATEGY_PUBLISH_CONFIG_INVALID"
