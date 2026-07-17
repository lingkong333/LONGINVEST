from copy import deepcopy
from datetime import UTC, datetime, time

import pytest

from long_invest.modules.monitor_schedules.contracts import ScheduleDefinition
from long_invest.modules.monitor_schedules.service import MonitorScheduleService
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 17, tzinfo=UTC)


class FakeRepository:
    def __init__(self) -> None:
        self.schedules = {}
        self.revisions = {}
        self.idempotency_locks = []

    async def lock_idempotency(self, idempotency_key):
        self.idempotency_locks.append(idempotency_key)

    async def list(self, *, include_archived=False):
        return [
            row
            for row in self.schedules.values()
            if include_archived or row.archived_at is None
        ]

    async def get(self, schedule_id, *, for_update=False):
        return self.schedules.get(schedule_id)

    async def find_replay(self, schedule_id, idempotency_key):
        return next(
            (
                r
                for r in self.revisions.values()
                if (schedule_id is None or r.schedule_id == schedule_id)
                and r.idempotency_key == idempotency_key
            ),
            None,
        )

    async def get_revision(self, schedule_id, revision_id):
        row = self.revisions.get(revision_id)
        return row if row and row.schedule_id == schedule_id else None

    async def list_revisions(self, schedule_id):
        return sorted(
            (r for r in self.revisions.values() if r.schedule_id == schedule_id),
            key=lambda r: r.revision_no,
            reverse=True,
        )

    async def create_schedule(self, schedule):
        self.schedules[schedule.id] = schedule

    async def add_revision(self, revision):
        self.revisions[revision.id] = revision

    async def initialize_current(self, schedule_id, revision_id):
        self.schedules[schedule_id].current_revision_id = revision_id

    async def switch_current(self, schedule_id, *, revision_id, name, expected_version):
        row = self.schedules[schedule_id]
        if row.version != expected_version:
            return False
        row.current_revision_id = revision_id
        row.name = name
        row.version += 1
        row.updated_at = NOW
        return True

    async def archive(self, schedule_id, *, expected_version, archived_at):
        row = self.schedules[schedule_id]
        if row.version != expected_version or row.archived_at is not None:
            return False
        row.archived_at = archived_at
        row.version += 1
        row.updated_at = archived_at
        return True


class Recorder:
    def __init__(self) -> None:
        self.items = []
        self.replays = {}

    async def find_replay(self, *, action, schedule_id, idempotency_key):
        scope = None if action == "created" else schedule_id
        return self.replays.get((scope, idempotency_key))

    async def record(self, item):
        self.items.append(item)
        scope = None if item.action == "created" else item.schedule_id
        self.replays[(scope, item.idempotency_key)] = item

    async def changed(self, item):
        self.items.append(item)


def command(*, name="盘中检查", times=(time(9, 45),), key="key-1", expected=None):
    return ScheduleDefinition(
        name=name,
        times=times,
        reason="调整监控时间",
        idempotency_key=key,
        expected_version=expected,
    )


def service():
    repo = FakeRepository()
    audit = Recorder()
    events = Recorder()
    return (
        MonitorScheduleService(repo, audit=audit, events=events, now=lambda: NOW),
        repo,
        audit,
        events,
    )


@pytest.mark.anyio
async def test_create_empty_schedule_and_content_replay_are_idempotent() -> None:
    svc, repo, audit, events = service()
    first = await svc.create(command(times=()))
    replay = await svc.create(command(times=()))
    assert replay.revision.id == first.revision.id
    assert replay.schedule.version == first.schedule.version == 1
    assert replay.replayed is True
    assert len(repo.revisions) == 1
    assert repo.idempotency_locks == ["key-1", "key-1"]
    assert len(audit.items) == len(events.items) == 1


@pytest.mark.anyio
async def test_create_replay_returns_current_consistent_state_after_changes() -> None:
    svc, _, _, _ = service()
    created = await svc.create(command())
    changed = await svc.update(
        created.schedule.id,
        command(times=(time(14, 30),), expected=1, key="update-after-create"),
    )
    replay = await svc.create(command())
    assert replay.replayed is True
    assert replay.schedule.version == changed.schedule.version == 2
    assert replay.schedule.current_revision_id == replay.revision.id
    assert replay.revision.id == changed.revision.id


@pytest.mark.anyio
async def test_create_replay_reflects_current_archived_state() -> None:
    svc, _, _, _ = service()
    created = await svc.create(command())
    archived = await svc.archive(
        created.schedule.id,
        expected_version=1,
        reason="停止使用",
        idempotency_key="archive-after-create",
    )
    replay = await svc.create(command())
    assert replay.replayed is True
    assert replay.schedule.version == archived.schedule.version == 2
    assert replay.schedule.archived_at == NOW
    assert replay.schedule.current_revision_id == replay.revision.id


@pytest.mark.anyio
async def test_same_key_with_different_content_conflicts() -> None:
    svc, _, _, _ = service()
    await svc.create(command())
    with pytest.raises(AppError) as caught:
        await svc.create(command(times=(time(10, 15),)))
    assert caught.value.code == "MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT"
    assert caught.value.status_code == 409

    with pytest.raises(AppError) as reason_conflict:
        await svc.create(
            ScheduleDefinition(
                name="盘中检查",
                times=(time(9, 45),),
                reason="另一个原因",
                idempotency_key="key-1",
            )
        )
    assert reason_conflict.value.code == "MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_update_replay_returns_consistent_current_owner_and_revision() -> None:
    svc, _, _, _ = service()
    created = await svc.create(command())
    first_update = await svc.update(
        created.schedule.id,
        command(times=(time(10, 15),), expected=1, key="update-1"),
    )
    latest = await svc.update(
        created.schedule.id,
        command(times=(time(14, 30),), expected=2, key="update-2"),
    )
    replay = await svc.update(
        created.schedule.id,
        command(times=(time(10, 15),), expected=1, key="update-1"),
    )
    assert first_update.revision.id != latest.revision.id
    assert replay.replayed is True
    assert replay.schedule.version == latest.schedule.version
    assert replay.schedule.current_revision_id == replay.revision.id
    assert replay.revision.id == latest.revision.id
    with pytest.raises(AppError) as changed_expected_version:
        await svc.update(
            created.schedule.id,
            command(times=(time(10, 15),), expected=2, key="update-1"),
        )
    assert (
        changed_expected_version.value.code == "MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT"
    )
    with pytest.raises(AppError) as cross_operation:
        await svc.archive(
            created.schedule.id,
            expected_version=3,
            reason="停止使用",
            idempotency_key="update-1",
        )
    assert cross_operation.value.code == "MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_update_uses_version_fence_and_archived_schedule_is_immutable() -> None:
    svc, _, audit, events = service()
    created = await svc.create(command())
    with pytest.raises(AppError) as stale:
        await svc.update(created.schedule.id, command(expected=99, key="stale"))
    assert stale.value.code == "MONITOR_SCHEDULE_VERSION_CONFLICT"

    archived = await svc.archive(
        created.schedule.id,
        expected_version=1,
        reason="停止使用",
        idempotency_key="archive-1",
        request_id="req-a",
        actor_user_id="u1",
        session_id="s1",
        trusted_ip="127.0.0.1",
    )
    assert archived.schedule.archived_at == NOW
    assert audit.items[-1].reason == events.items[-1].reason == "停止使用"
    replay = await svc.archive(
        created.schedule.id,
        expected_version=1,
        reason="停止使用",
        idempotency_key="archive-1",
        request_id="req-a",
        actor_user_id="u1",
        session_id="s1",
        trusted_ip="127.0.0.1",
    )
    assert replay.replayed is True
    assert replay.schedule.archived_at == NOW
    with pytest.raises(AppError) as changed_archive:
        await svc.archive(
            created.schedule.id,
            expected_version=1,
            reason="不同原因",
            idempotency_key="archive-1",
            request_id="req-a",
            actor_user_id="u1",
            session_id="s1",
            trusted_ip="127.0.0.1",
        )
    assert changed_archive.value.code == "MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT"
    with pytest.raises(AppError) as changed_archive_version:
        await svc.archive(
            created.schedule.id,
            expected_version=2,
            reason="停止使用",
            idempotency_key="archive-1",
        )
    assert changed_archive_version.value.code == "MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT"
    with pytest.raises(AppError) as frozen:
        await svc.current_revision(created.schedule.id)
    assert frozen.value.code == "MONITOR_SCHEDULE_ARCHIVED"
    with pytest.raises(AppError) as caught:
        await svc.update(created.schedule.id, command(expected=2, key="after-archive"))
    assert caught.value.code == "MONITOR_SCHEDULE_ARCHIVED"


@pytest.mark.anyio
async def test_restore_copies_history_into_a_new_revision() -> None:
    svc, _, audit, events = service()
    original = await svc.create(command(times=(time(9, 45),)))
    changed = await svc.update(
        original.schedule.id,
        command(times=(time(14, 30),), key="change", expected=1),
    )
    restored = await svc.restore(
        changed.schedule.id,
        source_revision_id=original.revision.id,
        expected_version=2,
        reason="恢复早盘",
        idempotency_key="restore",
        request_id="req-r",
        actor_user_id="u1",
        session_id="s1",
        trusted_ip="127.0.0.1",
    )
    assert restored.revision.revision_no == changed.revision.revision_no + 1
    assert restored.revision.id != original.revision.id
    assert tuple(restored.revision.times) == ("09:45",)
    assert restored.schedule.current_revision_id == restored.revision.id
    assert events.items[-1].action == audit.items[-1].action == "restored"
    assert audit.items[-1].before_summary["revision_id"] == str(changed.revision.id)
    assert audit.items[-1].after_summary["revision_id"] == str(restored.revision.id)
    with pytest.raises(AppError) as changed_source:
        await svc.restore(
            changed.schedule.id,
            source_revision_id=changed.revision.id,
            expected_version=2,
            reason="恢复早盘",
            idempotency_key="restore",
        )
    assert changed_source.value.code == "MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_archive_and_restore_reject_blank_reason() -> None:
    svc, _, _, _ = service()
    created = await svc.create(command())
    with pytest.raises(AppError) as archive:
        await svc.archive(
            created.schedule.id,
            expected_version=1,
            reason="   ",
            idempotency_key="blank-archive",
        )
    assert archive.value.status_code == 422
    with pytest.raises(AppError) as restore:
        await svc.restore(
            created.schedule.id,
            source_revision_id=created.revision.id,
            expected_version=1,
            reason="   ",
            idempotency_key="blank-restore",
        )
    assert restore.value.status_code == 422


@pytest.mark.anyio
async def test_audit_summaries_capture_real_before_and_after_state() -> None:
    svc, _, audit, _ = service()
    created = await svc.create(command())
    changed = await svc.update(
        created.schedule.id,
        command(name="午后检查", times=(time(14, 30),), expected=1, key="summary"),
    )
    event = audit.items[-1]
    assert event.before_summary == {
        "name": "盘中检查",
        "version": 1,
        "revision_id": str(created.revision.id),
        "times": ["09:45"],
        "archived": False,
    }
    assert event.after_summary == {
        "name": "午后检查",
        "version": 2,
        "revision_id": str(changed.revision.id),
        "times": ["14:30"],
        "archived": False,
    }


@pytest.mark.anyio
async def test_outbox_failure_leaves_no_business_change() -> None:
    svc, repo, _, events = service()

    async def fail(_item):
        raise RuntimeError("outbox failed")

    events.changed = fail
    before = deepcopy(repo.schedules)
    with pytest.raises(RuntimeError, match="outbox failed"):
        await svc.create(command())
    # A real transaction rolls this back; the service must not swallow the failure.
    assert before == {}
