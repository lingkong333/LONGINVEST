from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import (
    JobItemStatus,
    JobResult,
    JobRunStatus,
    JobStatus,
)
from long_invest.platform.jobs.service import JobService


class Session:
    def __init__(self):
        self.added = []

    def add_all(self, values):
        self.added.extend(values)

    async def flush(self):
        return None


class Repository:
    def __init__(self, parent, item=None, progress=(0, 1)):
        self.parent = parent
        self.item = item
        self.progress = progress
        self.keys = set()

    async def lock(self, _job_id):
        return self.parent

    async def item_keys(self, _job_id):
        return self.keys

    async def lock_item(self, _job_id, _item_key):
        return self.item

    async def item_progress(self, _job_id):
        return self.progress


class Service(JobService):
    def __init__(self, repository, active=True):
        self._session = Session()
        self._jobs = repository
        self.active = active

    async def _active_run(self, _job_id, _fence_token):
        return (object(), object()) if self.active else None


@pytest.mark.anyio
async def test_initialize_items_is_idempotent_but_rejects_scope_drift() -> None:
    parent = SimpleNamespace()
    repository = Repository(parent)
    service = Service(repository)
    job_id = uuid4()
    await service.initialize_items(job_id, ("600000.SH", "000001.SZ"))
    assert {item.item_key for item in service._session.added} == {
        "600000.SH",
        "000001.SZ",
    }

    repository.keys = {"600000.SH"}
    with pytest.raises(Exception) as error:
        await service.initialize_items(job_id, ("600000.SH", "000001.SZ"))
    assert error.value.code == "JOB_ITEM_SCOPE_CONFLICT"


@pytest.mark.anyio
async def test_finish_item_updates_parent_progress_only_with_active_child_fence() -> (
    None
):
    parent = SimpleNamespace(progress={}, updated_at=None, version=1)
    item = SimpleNamespace(
        status=JobItemStatus.PENDING,
        attempt_count=0,
        started_at=None,
        ended_at=None,
        result_ref=None,
        error_code=None,
        updated_at=None,
    )
    repository = Repository(parent, item, progress=(2, 2))
    service = Service(repository)
    completed, total, done = await service.finish_item(
        child_job_id=uuid4(),
        fence_token=uuid4(),
        parent_job_id=uuid4(),
        item_key="600000.SH",
        status=JobItemStatus.SUCCEEDED,
    )
    assert (completed, total, done) == (2, 2, True)
    assert item.status is JobItemStatus.SUCCEEDED
    assert parent.progress == {"completed": 2, "total": 2}

    rejected = Service(repository, active=False)
    with pytest.raises(Exception) as error:
        await rejected.finish_item(
            child_job_id=uuid4(),
            fence_token=uuid4(),
            parent_job_id=uuid4(),
            item_key="600000.SH",
            status=JobItemStatus.FAILED,
        )
    assert error.value.code == "JOB_FENCE_REJECTED"


@pytest.mark.anyio
async def test_finish_item_locks_child_run_before_parent() -> None:
    child_job_id = uuid4()
    parent_job_id = uuid4()
    run_id = uuid4()
    fence_token = uuid4()
    calls = []
    child = SimpleNamespace(
        current_fence_token=fence_token,
        current_run_id=run_id,
    )
    run = SimpleNamespace(
        fence_token=fence_token,
        status=JobRunStatus.RUNNING,
    )
    parent = SimpleNamespace(progress={}, updated_at=None, version=1)
    item = SimpleNamespace(
        status=JobItemStatus.PENDING,
        attempt_count=0,
        started_at=None,
        ended_at=None,
        result_ref=None,
        error_code=None,
        updated_at=None,
    )

    class OrderedRepository:
        async def lock(self, requested_id):
            calls.append(("job", requested_id))
            return child if requested_id == child_job_id else parent

        async def lock_run(self, requested_id):
            calls.append(("run", requested_id))
            return run

        async def lock_item(self, requested_id, item_key):
            calls.append(("item", requested_id, item_key))
            return item

        async def item_progress(self, requested_id):
            calls.append(("progress", requested_id))
            return 1, 1

    service = JobService(Session())
    service._jobs = OrderedRepository()
    service._outbox_writer = None

    await service.finish_item(
        child_job_id=child_job_id,
        fence_token=fence_token,
        parent_job_id=parent_job_id,
        item_key="600000.SH",
        status=JobItemStatus.SUCCEEDED,
    )

    assert calls[:3] == [
        ("job", child_job_id),
        ("run", run_id),
        ("job", parent_job_id),
    ]


@pytest.mark.anyio
async def test_last_successful_item_requests_completion_only_once() -> None:
    parent = SimpleNamespace(progress={}, updated_at=None, version=1)
    item = SimpleNamespace(
        status=JobItemStatus.PENDING,
        attempt_count=0,
        started_at=None,
        ended_at=None,
        result_ref=None,
        error_code=None,
        updated_at=None,
    )
    service = Service(Repository(parent, item, progress=(1, 1)))
    values = {
        "child_job_id": uuid4(),
        "fence_token": uuid4(),
        "parent_job_id": uuid4(),
        "item_key": "600000.SH",
        "status": JobItemStatus.SUCCEEDED,
    }

    first = await service.finish_item(**values)
    replay = await service.finish_item(**values)

    assert first == (1, 1, True)
    assert replay == (1, 1, False)
    assert item.attempt_count == 1


@pytest.mark.anyio
async def test_defer_keeps_parent_running_and_ends_run_successfully() -> None:
    job = SimpleNamespace(
        status=JobStatus.RUNNING,
        result_summary=None,
        current_run_id=uuid4(),
        current_fence_token=uuid4(),
        updated_at=None,
        version=4,
    )
    run = SimpleNamespace(
        status=JobRunStatus.RUNNING,
        ended_at=None,
        heartbeat_at=None,
    )

    class DeferredService(JobService):
        def __init__(self):
            self._session = Session()

        async def _active_run(self, _job_id, _fence_token):
            return job, run

    accepted = await DeferredService().defer(
        job_id=uuid4(),
        fence_token=uuid4(),
        result=JobResult(
            success=True,
            code="CHILDREN_PENDING",
            message="children created",
            retryable=False,
        ),
    )

    assert accepted is True
    assert job.status is JobStatus.RUNNING
    assert job.current_run_id is None
    assert job.current_fence_token is None
    assert run.status is JobRunStatus.SUCCEEDED
    assert run.ended_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "job_type", ["DAILY_DATA_COORDINATE", "BACKTEST_BULK"]
)
async def test_finalize_parent_reflects_partial_business_batch(job_type) -> None:
    parent = SimpleNamespace(
        job_type=job_type,
        status=JobStatus.RUNNING,
        result_summary=None,
        terminal_at=None,
        updated_at=None,
        version=1,
    )
    service = Service(Repository(parent))
    result = JobResult(
        success=True,
        code="PARTIAL",
        message="partial",
        retryable=False,
    )
    assert await service.finalize_parent(uuid4(), result) is True
    assert parent.status is JobStatus.PARTIAL
    assert parent.result_summary["code"] == "PARTIAL"
    assert parent.terminal_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("BACKTEST_BATCH_PAUSED", JobStatus.PAUSED),
        ("BACKTEST_BATCH_CANCELED", JobStatus.CANCELED),
    ],
)
async def test_finalize_backtest_parent_preserves_control_state(code, expected) -> None:
    parent = SimpleNamespace(
        job_type="BACKTEST_BULK",
        status=JobStatus.RUNNING,
        result_summary=None,
        terminal_at=None,
        updated_at=None,
        version=1,
    )
    result = JobResult(
        success=True,
        code=code,
        message="controlled",
        retryable=False,
    )

    assert await Service(Repository(parent)).finalize_parent(uuid4(), result) is True
    assert parent.status is expected
    assert (parent.terminal_at is None) is (expected is JobStatus.PAUSED)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "terminal_status",
    [
        JobStatus.SUCCEEDED,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.TIMED_OUT,
        JobStatus.LOST,
        JobStatus.CANCELED,
        JobStatus.REJECTED,
    ],
)
async def test_finalize_parent_preserves_any_existing_terminal_result(
    terminal_status,
) -> None:
    original = JobResult.success_result(data={"completed": 2}).as_dict()
    parent = SimpleNamespace(
        job_type="DAILY_DATA_COORDINATE",
        status=terminal_status,
        result_summary=original,
        terminal_at=object(),
        updated_at=object(),
        version=3,
    )
    service = Service(Repository(parent))

    accepted = await service.finalize_parent(
        uuid4(),
        JobResult.failure(
            code="LATE_FINALIZER_FAILED",
            message="late failure",
            retryable=False,
        ),
    )

    assert accepted is True
    assert parent.status is terminal_status
    assert parent.result_summary == original
    assert parent.version == 3


@pytest.mark.anyio
async def test_finalize_parent_rejects_terminal_job_with_invalid_parent_type() -> None:
    parent = SimpleNamespace(
        job_type="DAILY_DATA_FINALIZE",
        status=JobStatus.SUCCEEDED,
        result_summary={"code": "SUCCESS"},
        terminal_at=object(),
        updated_at=object(),
        version=2,
    )
    service = Service(Repository(parent))

    with pytest.raises(AppError) as captured:
        await service.finalize_parent(uuid4(), JobResult.success_result())

    assert captured.value.code == "JOB_PARENT_TYPE_INVALID"
