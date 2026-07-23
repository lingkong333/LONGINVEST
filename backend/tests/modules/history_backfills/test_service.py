from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.history_backfills.contracts import (
    CreateHistoryBackfill,
    FrozenHistoryScope,
    FrozenHistorySecurity,
    HistoryBackfillAuditContext,
    HistoryBackfillScope,
)
from long_invest.modules.history_backfills.service import (
    JOB_TYPE,
    QUEUE,
    HistoryBackfillService,
)
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.admin import JobCommandContext
from long_invest.platform.jobs.contracts import JobStatus


class ScopeSnapshots:
    def __init__(self) -> None:
        self.calls = 0

    async def freeze(self, _session, _command, *, owner_user_id):
        self.calls += 1
        assert owner_user_id
        return FrozenHistoryScope(
            snapshot_id=uuid4(),
            master_version=7,
            items=(
                FrozenHistorySecurity(uuid4(), "000001.SZ"),
                FrozenHistorySecurity(uuid4(), "600000.SH"),
            ),
        )


class Jobs:
    def __init__(self) -> None:
        self.job = None
        self.items = ()

    async def lock_submission(self, _scope, _key):
        return None

    async def find_submission(self, _scope, _key):
        return self.job

    async def submit(self, command):
        if self.job is None:
            self.job = SimpleNamespace(
                id=uuid4(),
                job_type=command.job_type,
                queue=command.queue,
                config_snapshot=command.config_snapshot,
                created_by_user_id=command.created_by_user_id,
                status=JobStatus.PENDING_DISPATCH,
                progress={"completed": 0, "total": 0},
                result_summary=None,
                version=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                terminal_at=None,
            )
        return self.job

    async def initialize_items(self, _job_id, item_keys):
        self.items = item_keys


class Admin:
    def __init__(self, job=None) -> None:
        self.job = job
        self.action = None

    async def get_job(self, _job_id):
        return self.job

    async def list_jobs(self, **filters):
        assert filters["job_type"] == JOB_TYPE
        assert filters["queue"] == QUEUE
        return SimpleNamespace(items=(), page=1, page_size=50, total=0)

    async def command(self, _job_id, action, _context):
        self.action = action
        return self.job

    async def allowed_actions(self, _job_id):
        return ("pause", "cancel", "retry-failed-items", "retry")


class Audit:
    def __init__(self) -> None:
        self.writes = []

    async def append(self, write):
        self.writes.append(write)


def command(*, concurrency: int = 4) -> CreateHistoryBackfill:
    return CreateHistoryBackfill(
        scope=HistoryBackfillScope.SELECTED,
        symbols=("600000.SH", "000001.SZ"),
        start_date=date(2010, 1, 1),
        end_date=date(2020, 12, 31),
        concurrency=concurrency,
    )


def context() -> HistoryBackfillAuditContext:
    return HistoryBackfillAuditContext(
        request_id="request-1",
        idempotency_key="idem-1",
        actor_user_id=str(uuid4()),
        session_id="session-1",
        trusted_ip="127.0.0.1",
        reason="补齐历史数据",
    )


@pytest.mark.anyio
async def test_create_freezes_scope_and_initializes_one_item_per_symbol() -> None:
    scopes = ScopeSnapshots()
    jobs = Jobs()
    audit = Audit()
    service = HistoryBackfillService(
        scope_snapshots=scopes,
        jobs=jobs,
        admin=Admin(),
        audit=audit,
    )

    job = await service.create(object(), command(), context(), owner_user_id=uuid4())

    assert job.job_type == JOB_TYPE
    assert job.queue == QUEUE
    assert jobs.items == ("000001.SZ", "600000.SH")
    assert job.config_snapshot["start_date"] == "2010-01-01"
    assert job.config_snapshot["end_date"] == "2020-12-31"
    assert job.config_snapshot["concurrency"] == 4
    assert len(audit.writes) == 1


@pytest.mark.anyio
async def test_create_replay_does_not_freeze_scope_again() -> None:
    scopes = ScopeSnapshots()
    jobs = Jobs()
    service = HistoryBackfillService(
        scope_snapshots=scopes,
        jobs=jobs,
        admin=Admin(),
        audit=Audit(),
    )
    audit_context = context()
    owner = uuid4()
    first = await service.create(
        object(), command(), audit_context, owner_user_id=owner
    )
    second = await service.create(
        object(), command(), audit_context, owner_user_id=owner
    )

    assert first is second
    assert scopes.calls == 1


@pytest.mark.anyio
async def test_reused_idempotency_key_rejects_changed_request() -> None:
    service = HistoryBackfillService(
        scope_snapshots=ScopeSnapshots(),
        jobs=Jobs(),
        admin=Admin(),
        audit=Audit(),
    )
    audit_context = context()
    owner = uuid4()
    await service.create(object(), command(), audit_context, owner_user_id=owner)

    with pytest.raises(AppError) as raised:
        await service.create(
            object(), command(concurrency=5), audit_context, owner_user_id=owner
        )
    assert raised.value.code == "HISTORY_BACKFILL_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_get_rejects_job_from_another_module() -> None:
    foreign = SimpleNamespace(job_type="OTHER", queue=QUEUE)
    service = HistoryBackfillService(
        scope_snapshots=ScopeSnapshots(),
        jobs=Jobs(),
        admin=Admin(foreign),
        audit=Audit(),
    )
    with pytest.raises(AppError) as raised:
        await service.get(uuid4())
    assert raised.value.code == "HISTORY_BACKFILL_NOT_FOUND"


@pytest.mark.anyio
async def test_retry_failed_maps_to_job_item_control() -> None:
    job = SimpleNamespace(job_type=JOB_TYPE, queue=QUEUE)
    admin = Admin(job)
    service = HistoryBackfillService(
        scope_snapshots=ScopeSnapshots(), jobs=Jobs(), admin=admin, audit=Audit()
    )
    await service.command(
        uuid4(),
        "retry-failed",
        JobCommandContext(
            request_id="request-2",
            idempotency_key="idem-2",
            actor_user_id=str(uuid4()),
            reason="重试失败股票",
            expected_version=3,
        ),
    )
    assert admin.action == "retry-failed-items"


@pytest.mark.anyio
async def test_allowed_actions_use_job_state_and_hide_generic_retry() -> None:
    job = SimpleNamespace(job_type=JOB_TYPE, queue=QUEUE)
    service = HistoryBackfillService(
        scope_snapshots=ScopeSnapshots(),
        jobs=Jobs(),
        admin=Admin(job),
        audit=Audit(),
    )

    actions = await service.allowed_actions(uuid4())

    assert [item.value for item in actions] == [
        "PAUSE",
        "CANCEL",
        "RETRY_FAILED",
    ]


@pytest.mark.anyio
async def test_allowed_actions_many_reuses_the_same_admin_service() -> None:
    job = SimpleNamespace(job_type=JOB_TYPE, queue=QUEUE)
    service = HistoryBackfillService(
        scope_snapshots=ScopeSnapshots(),
        jobs=Jobs(),
        admin=Admin(job),
        audit=Audit(),
    )
    first_id = uuid4()
    second_id = uuid4()

    actions = await service.allowed_actions_many((first_id, second_id))

    assert set(actions) == {first_id, second_id}
    assert all(
        [item.value for item in value] == [
            "PAUSE",
            "CANCEL",
            "RETRY_FAILED",
        ]
        for value in actions.values()
    )
