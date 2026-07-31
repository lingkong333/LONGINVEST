from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

import long_invest.bootstrap.backtest_postgres_jobs as jobs_module
from long_invest.modules.backtests.contracts import BacktestMode
from long_invest.platform.jobs.contracts import JobExecutionContext, JobStatus


class Database:
    @asynccontextmanager
    async def transaction(self):
        yield object()


class ProgressJobs:
    reports = []

    def __init__(self, _session):
        pass

    async def report_progress(self, _job_id, _token, **values):
        self.reports.append(values)
        return JobStatus.RUNNING

    async def command(self, _job_id, _action):
        raise AssertionError("completed batch must not request a control action")


class Application:
    def __init__(self):
        self.recovered = []
        self.executed = []
        self.entries = tuple(
            SimpleNamespace(
                symbol=f"00000{index}.SZ",
                security_id=uuid4(),
            )
            for index in range(1, 6)
        )
        self.summary_calls = 0

    async def get_execution(self, _task_id):
        return SimpleNamespace(
            task=SimpleNamespace(
                mode=BacktestMode.MARKET,
                universe_snapshot=self.entries,
            )
        )

    async def recover(self, _task_id, *, item_id, **_values):
        self.recovered.append(item_id)

    async def run_item(self, _task_id, *, item_id, **_values):
        self.executed.append(item_id)

    async def get_summary(self, task_id):
        self.summary_calls += 1
        status = "RUNNING" if self.summary_calls == 1 else "PARTIAL"
        return SimpleNamespace(
            task_id=task_id,
            status=SimpleNamespace(value=status),
            succeeded_items=4,
            failed_items=1,
            model_dump=lambda **_kwargs: {
                "task_id": str(task_id),
                "status": status,
            },
        )


@pytest.mark.anyio
async def test_batch_resumes_from_checkpoint_without_creating_child_jobs(
    monkeypatch,
) -> None:
    application = Application()
    ProgressJobs.reports = []
    monkeypatch.setattr(
        jobs_module, "build_backtest_application", lambda: application
    )
    monkeypatch.setattr(jobs_module, "get_database", Database)
    monkeypatch.setattr(jobs_module, "PostgresJobService", ProgressJobs)
    task_id = uuid4()
    context = JobExecutionContext(
        job_id=uuid4(),
        fence_token=uuid4(),
        config={
            "backtest_task_id": str(task_id),
            "generation": 1,
            "recover": False,
            "mode": "MARKET",
            "item_keys": [entry.symbol for entry in application.entries],
            "concurrency": 2,
        },
        checkpoint={"next_index": 2},
    )

    result = await jobs_module.backtest_batch(context)

    assert result.success is True
    assert result.code == "PARTIAL"
    assert len(application.recovered) == 2
    assert len(application.executed) == 1
    assert [report["progress"].completed for report in ProgressJobs.reports] == [
        4,
        5,
    ]
    assert [report["checkpoint"] for report in ProgressJobs.reports] == [
        {"next_index": 4},
        {"next_index": 5},
    ]
