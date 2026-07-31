from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.system_status.adapters import (
    PostgresRuntimeStatusAdapter,
    RqRuntimeStatusAdapter,
    SchedulerStatusAdapter,
)
from long_invest.modules.system_status.contracts import HealthStatus
from long_invest.platform.jobs.contracts import JobStatus


@pytest.mark.anyio
async def test_scheduler_does_not_infer_heartbeat_from_business_occurrence() -> None:
    runtime = SimpleNamespace(get=_async_value(None))
    adapter = SchedulerStatusAdapter(SimpleNamespace(), SimpleNamespace(), runtime)
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    adapter._database_time = _async_value(now)  # type: ignore[method-assign]

    result = await adapter.get_status()

    assert result.status is HealthStatus.UNKNOWN
    assert result.last_scan_at is None
    assert result.database_time == now
    assert result.automatic_scheduling_paused is True
    assert result.pause_reason == "scheduler heartbeat is not available"


@pytest.mark.anyio
async def test_scheduler_reports_fresh_heartbeat_as_healthy() -> None:
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    runtime = SimpleNamespace(
        get=_async_value(
            SimpleNamespace(
                heartbeat_at=now,
                last_scan_at=now,
                consecutive_failures=0,
                automatic_scheduling_paused=False,
                pause_reason=None,
                intraday_plan=(
                    {
                        "key": "2026-07-22T14:30",
                        "next_run_at": "2026-07-22T06:30:00+00:00",
                        "timezone": "Asia/Shanghai",
                    },
                ),
                persistent_plan=(
                    {
                        "key": "daily-market-data",
                        "next_run_at": "2026-07-22T09:00:00+00:00",
                        "timezone": "Asia/Shanghai",
                    },
                ),
            )
        )
    )
    adapter = SchedulerStatusAdapter(SimpleNamespace(), SimpleNamespace(), runtime)
    adapter._database_time = _async_value(now)  # type: ignore[method-assign]

    result = await adapter.get_status()

    assert result.status is HealthStatus.HEALTHY
    assert result.last_scan_at == now
    assert result.automatic_scheduling_paused is False
    assert [plan.kind for plan in result.plans] == ["INTRADAY", "PERSISTENT"]
    assert result.next_run_at == datetime(2026, 7, 22, 6, 30, tzinfo=UTC)


@pytest.mark.anyio
async def test_scheduler_reports_stale_heartbeat_as_unavailable() -> None:
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    runtime = SimpleNamespace(
        get=_async_value(
            SimpleNamespace(
                heartbeat_at=now - timedelta(seconds=31),
                last_scan_at=now - timedelta(seconds=31),
                consecutive_failures=0,
                automatic_scheduling_paused=False,
                pause_reason=None,
            )
        )
    )
    adapter = SchedulerStatusAdapter(SimpleNamespace(), SimpleNamespace(), runtime)
    adapter._database_time = _async_value(now)  # type: ignore[method-assign]

    result = await adapter.get_status()

    assert result.status is HealthStatus.UNAVAILABLE
    assert result.automatic_scheduling_paused is True
    assert result.pause_reason == "scheduler heartbeat is stale"


def test_rq_worker_state_uses_enum_value(monkeypatch) -> None:
    class Connection:
        def close(self) -> None:
            pass

    worker = SimpleNamespace(
        name="worker-1",
        queues=(SimpleNamespace(name="default"),),
        state=SimpleNamespace(value="idle"),
        birth_date=None,
        last_heartbeat=None,
        successful_job_count=2,
        failed_job_count=1,
        _job_id=None,
    )
    monkeypatch.setattr(
        "long_invest.modules.system_status.adapters.Redis.from_url",
        lambda _url: Connection(),
    )
    monkeypatch.setattr(
        "long_invest.modules.system_status.adapters.Worker.all",
        lambda connection: (worker,),
    )

    result = RqRuntimeStatusAdapter("redis://unused")._workers()

    assert result[0].status == "IDLE"
    assert result[0].processed_jobs == 2
    assert result[0].failed_jobs == 1


@pytest.mark.anyio
async def test_postgres_runtime_reports_the_single_background_and_queue(
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    current_job_id = uuid4()
    totals = {
        JobStatus.RUNNING: 1,
        JobStatus.PENDING: 3,
        JobStatus.SUCCEEDED: 7,
        JobStatus.FAILED: 2,
    }

    class Jobs:
        def __init__(self, _session) -> None:
            pass

        async def list_jobs(self, *, status, **_filters):
            items = (
                (SimpleNamespace(id=current_job_id),)
                if status is JobStatus.RUNNING
                else ()
            )
            return SimpleNamespace(items=items, total=totals[status])

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    database = SimpleNamespace(session=lambda: SessionContext())
    runtime = SimpleNamespace(
        get=_async_value(SimpleNamespace(heartbeat_at=now))
    )
    monkeypatch.setattr(
        "long_invest.modules.system_status.adapters.JobAdminService",
        Jobs,
    )
    adapter = PostgresRuntimeStatusAdapter(database, runtime)

    workers = await adapter.list_workers()
    queues = await adapter.list_queues()

    assert workers[0].worker_id == "ordinary-background"
    assert workers[0].status == "RUNNING"
    assert workers[0].current_job_id == current_job_id
    assert workers[0].processed_jobs == 7
    assert workers[0].failed_jobs == 2
    assert queues[0].name == "postgres"
    assert queues[0].depth == 3
    assert queues[0].active_workers == 1


@pytest.mark.anyio
async def test_postgres_runtime_reports_stale_background_as_unavailable(
    monkeypatch,
) -> None:
    class Jobs:
        def __init__(self, _session) -> None:
            pass

        async def list_jobs(self, **_filters):
            return SimpleNamespace(items=(), total=0)

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    database = SimpleNamespace(session=lambda: SessionContext())
    runtime = SimpleNamespace(
        get=_async_value(
            SimpleNamespace(
                heartbeat_at=datetime.now(UTC) - timedelta(seconds=31)
            )
        )
    )
    monkeypatch.setattr(
        "long_invest.modules.system_status.adapters.JobAdminService",
        Jobs,
    )
    adapter = PostgresRuntimeStatusAdapter(database, runtime)

    assert (await adapter.list_workers())[0].status == "UNAVAILABLE"
    assert (await adapter.list_queues())[0].active_workers == 0


def _async_value(value):
    async def read():
        return value

    return read
