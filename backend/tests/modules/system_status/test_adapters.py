from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from long_invest.modules.system_status.adapters import (
    RqRuntimeStatusAdapter,
    SchedulerStatusAdapter,
)
from long_invest.modules.system_status.contracts import HealthStatus


@pytest.mark.anyio
async def test_scheduler_does_not_infer_heartbeat_from_business_occurrence() -> None:
    adapter = SchedulerStatusAdapter(SimpleNamespace(), SimpleNamespace())
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    adapter._database_time = _async_value(now)  # type: ignore[method-assign]

    result = await adapter.get_status()

    assert result.status is HealthStatus.UNKNOWN
    assert result.last_scan_at is None
    assert result.database_time == now
    assert result.pause_reason == "scheduler heartbeat is not available"


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
