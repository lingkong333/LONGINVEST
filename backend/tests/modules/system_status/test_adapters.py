from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from long_invest.modules.system_status.adapters import (
    RqRuntimeStatusAdapter,
    SchedulerStatusAdapter,
)
from long_invest.modules.system_status.contracts import HealthStatus


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
            )
        )
    )
    adapter = SchedulerStatusAdapter(SimpleNamespace(), SimpleNamespace(), runtime)
    adapter._database_time = _async_value(now)  # type: ignore[method-assign]

    result = await adapter.get_status()

    assert result.status is HealthStatus.HEALTHY
    assert result.last_scan_at == now
    assert result.automatic_scheduling_paused is False


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


def _async_value(value):
    async def read():
        return value

    return read
