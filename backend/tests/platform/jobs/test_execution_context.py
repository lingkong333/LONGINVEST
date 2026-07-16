from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.platform.jobs import worker
from long_invest.platform.jobs.contracts import JobExecutionContext, JobResult


@pytest.mark.anyio
async def test_worker_passes_job_identity_fence_and_frozen_config(monkeypatch) -> None:
    job_id = uuid4()
    fence_token = uuid4()
    job = SimpleNamespace(
        id=job_id,
        job_type="CONTEXT_TEST",
        config_snapshot={"symbol": "600000.SH"},
        soft_timeout_seconds=60,
    )
    received: list[JobExecutionContext] = []

    async def handler(context: JobExecutionContext) -> JobResult:
        received.append(context)
        return JobResult.success_result(data=dict(context.config))

    class FakeSession:
        async def get(self, _model, requested_id):
            return job if requested_id == job_id else None

    class FakeDatabase:
        def __init__(self, _url: str) -> None:
            pass

        @asynccontextmanager
        async def transaction(self):
            yield FakeSession()

        async def dispose(self) -> None:
            pass

    class FakeJobService:
        def __init__(self, _session) -> None:
            pass

        async def claim(self, *, job_id, worker_id):
            del job_id, worker_id
            return SimpleNamespace(fence_token=fence_token)

        async def start(self, *, job_id, fence_token):
            del job_id, fence_token
            return True

        async def complete(self, *, job_id, fence_token, result):
            del job_id, fence_token, result
            return True

    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeJobService)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(database_url="unused")
    )
    monkeypatch.setitem(worker.HANDLERS, "CONTEXT_TEST", handler)

    result = await worker._execute_job(job_id, uuid4())

    assert result.success is True
    assert len(received) == 1
    assert received[0].job_id == job_id
    assert received[0].fence_token == fence_token
    assert received[0].config == {"symbol": "600000.SH"}


@pytest.mark.anyio
async def test_worker_cooperatively_cancels_at_frozen_soft_timeout(
    monkeypatch,
) -> None:
    job_id = uuid4()
    fence_token = uuid4()
    job = SimpleNamespace(
        id=job_id,
        job_type="TIMEOUT_TEST",
        config_snapshot={},
        soft_timeout_seconds=0.01,
    )
    timed_out: list[str] = []

    async def handler(_context: JobExecutionContext) -> JobResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    class FakeSession:
        async def get(self, _model, requested_id):
            return job if requested_id == job_id else None

    class FakeDatabase:
        def __init__(self, _url: str) -> None:
            pass

        @asynccontextmanager
        async def transaction(self):
            yield FakeSession()

        async def dispose(self) -> None:
            pass

    class FakeJobService:
        def __init__(self, _session) -> None:
            pass

        async def claim(self, *, job_id, worker_id):
            del job_id, worker_id
            return SimpleNamespace(fence_token=fence_token)

        async def start(self, **_kwargs):
            return True

        async def timeout(self, *, result, **_kwargs):
            timed_out.append(result.code)
            return True

    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeJobService)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(database_url="unused")
    )
    monkeypatch.setitem(worker.HANDLERS, "TIMEOUT_TEST", handler)

    result = await worker._execute_job(job_id, uuid4())

    assert result.code == "JOB_SOFT_TIMEOUT"
    assert timed_out == ["JOB_SOFT_TIMEOUT"]


@pytest.mark.anyio
async def test_worker_bounded_cleanup_finishes_before_hard_timeout_budget(
    monkeypatch,
) -> None:
    job_id = uuid4()
    fence_token = uuid4()
    cleanup_seconds = 0.02
    job = SimpleNamespace(
        id=job_id,
        job_type="BOUNDED_CLEANUP_TEST",
        config_snapshot={},
        soft_timeout_seconds=0.01,
        hard_timeout_seconds=0.5,
    )
    cleanup_finished = asyncio.Event()

    async def handler(_context: JobExecutionContext) -> JobResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(cleanup_seconds)
            cleanup_finished.set()
            raise

    class FakeSession:
        async def get(self, _model, requested_id):
            return job if requested_id == job_id else None

    class FakeDatabase:
        def __init__(self, _url: str) -> None:
            pass

        @asynccontextmanager
        async def transaction(self):
            yield FakeSession()

        async def dispose(self) -> None:
            pass

    class FakeJobService:
        def __init__(self, _session) -> None:
            pass

        async def claim(self, **_kwargs):
            return SimpleNamespace(fence_token=fence_token)

        async def start(self, **_kwargs):
            return True

        async def timeout(self, **_kwargs):
            return True

    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeJobService)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(database_url="unused")
    )
    monkeypatch.setitem(worker.HANDLERS, "BOUNDED_CLEANUP_TEST", handler)

    started = monotonic()
    result = await worker._execute_job(job_id, uuid4())
    elapsed = monotonic() - started

    assert job.soft_timeout_seconds + cleanup_seconds < job.hard_timeout_seconds
    assert result.code == "JOB_SOFT_TIMEOUT"
    assert cleanup_finished.is_set()
    assert elapsed < job.hard_timeout_seconds


@pytest.mark.anyio
async def test_worker_heartbeats_while_a_long_handler_is_running(monkeypatch) -> None:
    job_id = uuid4()
    fence_token = uuid4()
    job = SimpleNamespace(
        id=job_id,
        job_type="HEARTBEAT_TEST",
        config_snapshot={},
        soft_timeout_seconds=1,
    )
    heartbeats = []

    async def handler(_context):
        await asyncio.sleep(0.04)
        return JobResult.success_result()

    class FakeSession:
        async def get(self, _model, requested_id):
            return job if requested_id == job_id else None

    class FakeDatabase:
        def __init__(self, _url):
            pass

        @asynccontextmanager
        async def transaction(self):
            yield FakeSession()

        async def dispose(self):
            pass

    class FakeJobService:
        def __init__(self, _session):
            pass

        async def claim(self, **_kwargs):
            return SimpleNamespace(fence_token=fence_token)

        async def start(self, **_kwargs):
            return True

        async def heartbeat(self, **_kwargs):
            heartbeats.append(True)
            return True

        async def complete(self, **_kwargs):
            return True

    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeJobService)
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="unused", job_heartbeat_interval_seconds=0.01
        ),
    )
    monkeypatch.setitem(worker.HANDLERS, "HEARTBEAT_TEST", handler)

    result = await worker._execute_job(job_id, uuid4())

    assert result.success is True
    assert len(heartbeats) >= 2


@pytest.mark.anyio
async def test_worker_keeps_heartbeating_after_one_database_failure(
    monkeypatch,
) -> None:
    stop = asyncio.Event()
    attempts = 0

    class FakeDatabase:
        @asynccontextmanager
        async def transaction(self):
            yield object()

    class FakeJobService:
        def __init__(self, _session):
            pass

        async def heartbeat(self, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary database error")
            stop.set()
            return True

    monkeypatch.setattr(worker, "JobService", FakeJobService)

    await asyncio.wait_for(
        worker._heartbeat_loop(
            FakeDatabase(),
            uuid4(),
            uuid4(),
            stop,
            interval_seconds=0.001,
        ),
        timeout=0.1,
    )

    assert attempts == 2


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["failure", "timeout"])
async def test_worker_closes_linked_parent_when_finalize_job_fails(
    monkeypatch, mode
) -> None:
    job_id = uuid4()
    parent_job_id = uuid4()
    fence_token = uuid4()
    job = SimpleNamespace(
        id=job_id,
        job_type="FINALIZE_FAILURE_TEST",
        config_snapshot={"linked_parent_job_id": str(parent_job_id)},
        soft_timeout_seconds=0.01,
    )
    calls = []

    async def handler(_context):
        if mode == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("finalize failed")

    class FakeSession:
        async def get(self, _model, requested_id):
            return job if requested_id == job_id else None

    class FakeDatabase:
        def __init__(self, _url):
            pass

        @asynccontextmanager
        async def transaction(self):
            yield FakeSession()

        async def dispose(self):
            pass

    class FakeJobService:
        def __init__(self, _session):
            pass

        async def claim(self, **_kwargs):
            return SimpleNamespace(fence_token=fence_token)

        async def start(self, **_kwargs):
            return True

        async def finalize_parent(self, parent_id, result):
            calls.append(("parent", parent_id, result.code))
            return True

        async def fail(self, **_kwargs):
            calls.append(("fail",))
            return True

        async def timeout(self, **_kwargs):
            calls.append(("timeout",))
            return True

    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeJobService)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(database_url="unused")
    )
    monkeypatch.setitem(worker.HANDLERS, "FINALIZE_FAILURE_TEST", handler)

    result = await worker._execute_job(job_id, uuid4())

    expected_code = "JOB_SOFT_TIMEOUT" if mode == "timeout" else "JOB_HANDLER_FAILED"
    lifecycle_method = "timeout" if mode == "timeout" else "fail"
    assert result.code == expected_code
    assert calls == [
        (lifecycle_method,),
        ("parent", parent_job_id, expected_code),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["failure", "timeout"])
async def test_worker_does_not_close_linked_parent_when_old_fence_is_rejected(
    monkeypatch, mode
) -> None:
    job_id = uuid4()
    parent_job_id = uuid4()
    fence_token = uuid4()
    job = SimpleNamespace(
        id=job_id,
        job_type="STALE_FINALIZE_FAILURE_TEST",
        config_snapshot={"linked_parent_job_id": str(parent_job_id)},
        soft_timeout_seconds=1,
    )
    parent_results = []

    async def handler(_context):
        if mode == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("finalize failed")

    class FakeSession:
        async def get(self, _model, requested_id):
            return job if requested_id == job_id else None

    class FakeDatabase:
        def __init__(self, _url):
            pass

        @asynccontextmanager
        async def transaction(self):
            yield FakeSession()

        async def dispose(self):
            pass

    class FakeJobService:
        def __init__(self, _session):
            pass

        async def claim(self, **_kwargs):
            return SimpleNamespace(fence_token=fence_token)

        async def start(self, **_kwargs):
            return True

        async def fail(self, **_kwargs):
            return False

        async def timeout(self, **_kwargs):
            return False

        async def finalize_parent(self, parent_id, result):
            parent_results.append((parent_id, result.code))
            return True

    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeJobService)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(database_url="unused")
    )
    monkeypatch.setitem(worker.HANDLERS, "STALE_FINALIZE_FAILURE_TEST", handler)

    result = await worker._execute_job(job_id, uuid4())

    expected_code = "JOB_SOFT_TIMEOUT" if mode == "timeout" else "JOB_HANDLER_FAILED"
    assert result.code == expected_code
    assert parent_results == []


@pytest.mark.anyio
async def test_worker_defers_parent_while_children_are_pending(monkeypatch) -> None:
    job_id = uuid4()
    fence_token = uuid4()
    job = SimpleNamespace(
        id=job_id,
        job_type="CHILDREN_PENDING_TEST",
        config_snapshot={},
        soft_timeout_seconds=1,
    )
    lifecycle = []

    async def handler(_context):
        return JobResult(
            success=True,
            code="CHILDREN_PENDING",
            message="children created",
            retryable=False,
        )

    class FakeSession:
        async def get(self, _model, requested_id):
            return job if requested_id == job_id else None

    class FakeDatabase:
        def __init__(self, _url):
            pass

        @asynccontextmanager
        async def transaction(self):
            yield FakeSession()

        async def dispose(self):
            pass

    class FakeJobService:
        def __init__(self, _session):
            pass

        async def claim(self, **_kwargs):
            return SimpleNamespace(fence_token=fence_token)

        async def start(self, **_kwargs):
            return True

        async def defer(self, **_kwargs):
            lifecycle.append("defer")
            return True

        async def complete(self, **_kwargs):
            lifecycle.append("complete")
            return True

    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeJobService)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(database_url="unused")
    )
    monkeypatch.setitem(worker.HANDLERS, "CHILDREN_PENDING_TEST", handler)

    result = await worker._execute_job(job_id, uuid4())

    assert result.code == "CHILDREN_PENDING"
    assert lifecycle == ["defer"]


@pytest.mark.anyio
async def test_worker_closes_linked_parent_item_when_child_handler_fails(
    monkeypatch,
) -> None:
    job_id = uuid4()
    fence_token = uuid4()
    parent_job_id = uuid4()
    completion = {
        "job_type": "DAILY_DATA_FINALIZE",
        "queue": "daily-market-data",
        "idempotency_scope": "daily:finish",
        "idempotency_key": "batch-1",
        "request_id": "request-1",
        "config_snapshot": {},
        "soft_timeout_seconds": 30,
        "hard_timeout_seconds": 60,
    }
    job = SimpleNamespace(
        id=job_id,
        job_type="LINKED_FAILURE_TEST",
        config_snapshot={
            "linked_item": {
                "parent_job_id": str(parent_job_id),
                "item_key": "600000.SH",
                "completion_job": completion,
            }
        },
        soft_timeout_seconds=1,
    )
    calls = []

    async def handler(_context):
        raise RuntimeError("failed")

    class FakeSession:
        async def get(self, _model, requested_id):
            return job if requested_id == job_id else None

    class FakeDatabase:
        def __init__(self, _url):
            pass

        @asynccontextmanager
        async def transaction(self):
            yield FakeSession()

        async def dispose(self):
            pass

    class FakeJobService:
        def __init__(self, _session):
            pass

        async def claim(self, **_kwargs):
            return SimpleNamespace(fence_token=fence_token)

        async def start(self, **_kwargs):
            return True

        async def finish_item(self, **kwargs):
            calls.append(("item", kwargs))
            return 1, 1, True

        async def submit(self, command):
            calls.append(("submit", command))

        async def fail(self, **_kwargs):
            calls.append(("fail", None))
            return True

    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeJobService)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(database_url="unused")
    )
    monkeypatch.setitem(worker.HANDLERS, "LINKED_FAILURE_TEST", handler)

    result = await worker._execute_job(job_id, uuid4())

    assert result.code == "JOB_HANDLER_FAILED"
    assert [name for name, _value in calls] == ["item", "submit", "fail"]
    assert calls[0][1]["parent_job_id"] == parent_job_id
