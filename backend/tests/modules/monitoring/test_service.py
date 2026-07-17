from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.modules.monitoring.contracts import (
    FrozenSubscription,
    SubscriptionStatus,
)
from long_invest.modules.monitoring.service import (
    MonitorSubscriptionService,
    SubscriptionAuditContext,
    SubscriptionConfig,
)
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 17, tzinfo=UTC)


class Repository:
    def __init__(self):
        self.session = object()
        self.owners = {}
        self.revisions = {}

    async def lock_security(self, security_id):
        pass

    async def find_open_by_security(self, security_id):
        return next(
            (
                x
                for x in self.owners.values()
                if x.security_id == security_id and x.archived_at is None
            ),
            None,
        )

    async def get(self, subscription_id, *, for_update=False):
        return self.owners.get(subscription_id)

    async def get_revision(self, subscription_id, revision_id):
        row = self.revisions.get(revision_id)
        return row if row and row.subscription_id == subscription_id else None

    async def list_revisions(self, subscription_id):
        return [
            x for x in self.revisions.values() if x.subscription_id == subscription_id
        ]

    async def create(self, owner):
        self.owners[owner.id] = owner

    async def add_revision(self, revision):
        self.revisions[revision.id] = revision

    async def initialize_current(self, subscription_id, revision_id):
        self.owners[subscription_id].current_revision_id = revision_id

    async def transition(
        self,
        subscription_id,
        *,
        expected_status,
        expected_version,
        status,
        archived_at=None,
    ):
        row = self.owners[subscription_id]
        if str(row.status) != str(expected_status) or row.version != expected_version:
            return False
        row.status = status
        row.version = expected_version + 1
        row.archived_at = archived_at
        return True

    async def switch_revision(self, subscription_id, *, revision_id, expected_version):
        row = self.owners[subscription_id]
        if row.version != expected_version or row.archived_at is not None:
            return False
        row.current_revision_id = revision_id
        row.version = expected_version + 1
        return True


class Recorder:
    def __init__(self):
        self.items = []
        self.replays = {}

    async def find_replay(self, *, subscription_id, idempotency_key):
        return self.replays.get((subscription_id, idempotency_key))

    async def record(self, event):
        self.items.append(event)
        self.replays[
            (
                event.subscription_id if event.action != "created" else None,
                event.idempotency_key,
            )
        ] = event

    async def publish(self, event):
        self.items.append(event)


class Readiness:
    def __init__(self, value=False):
        self.value = value

    async def current_readiness(self, subscription_id):
        return self.value

    async def published_version(self, strategy_version_id):
        return self.value


def config(**changes):
    values = dict(
        schedule_id=None,
        schedule_revision_id=None,
        target_mode="MANUAL",
        target_version_id=None,
        strategy_version_id=None,
        parameters={},
        hysteresis_ratio=Decimal("0.010000"),
        hysteresis_min=Decimal("0.050000"),
        notification_mode="DEFAULT",
        reason="创建订阅",
        idempotency_key="sub-1",
    )
    values.update(changes)
    return SubscriptionConfig(**values)


def service(*, ready=False):
    repo = Repository()
    audit = Recorder()
    events = Recorder()
    return (
        MonitorSubscriptionService(
            repo,
            audit=audit,
            events=events,
            target_readiness=Readiness(ready),
            strategy_readiness=Readiness(ready),
            now=lambda: NOW,
        ),
        repo,
        audit,
        events,
    )


@pytest.mark.anyio
async def test_create_reuses_open_subscription_and_freezes_schedule() -> None:
    svc, repo, audit, events = service()
    security_id = uuid4()
    schedule_id = uuid4()
    schedule_revision_id = uuid4()
    context = SubscriptionAuditContext(
        request_id="req-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )
    first = await svc.create(
        security_id=security_id,
        symbol="600000.SH",
        config=config(
            schedule_id=schedule_id, schedule_revision_id=schedule_revision_id
        ),
        audit_context=context,
    )
    replay = await svc.create(
        security_id=security_id,
        symbol="600000.SH",
        config=config(
            schedule_id=schedule_id, schedule_revision_id=schedule_revision_id
        ),
    )
    assert first.subscription.status == SubscriptionStatus.CONFIGURING
    assert replay.subscription.id == first.subscription.id
    assert replay.replayed is True
    assert replay.revision.schedule_revision_id == schedule_revision_id
    assert len(repo.owners) == len(repo.revisions) == 1
    assert len(audit.items) == len(events.items) == 1
    assert audit.items[0].request_id == "req-1"
    assert audit.items[0].actor_user_id == "user-1"


@pytest.mark.anyio
async def test_same_key_different_content_conflicts() -> None:
    svc, _, _, _ = service()
    security_id = uuid4()
    await svc.create(security_id=security_id, symbol="600000.SH", config=config())
    with pytest.raises(AppError) as caught:
        await svc.create(
            security_id=security_id,
            symbol="600000.SH",
            config=config(notification_mode="CUSTOM"),
        )
    assert caught.value.code == "MONITOR_SUBSCRIPTION_CONFLICT"


@pytest.mark.anyio
async def test_enable_requires_target_and_lifecycle_is_version_fenced() -> None:
    svc, _, _, _ = service()
    created = await svc.create(security_id=uuid4(), symbol="600000.SH", config=config())
    with pytest.raises(AppError) as not_ready:
        await svc.enable(
            created.subscription.id,
            expected_version=1,
            reason="启用",
            idempotency_key="enable-1",
        )
    assert not_ready.value.code == "MONITOR_SUBSCRIPTION_NOT_READY"
    with pytest.raises(AppError) as stale_not_ready:
        await svc.enable(
            created.subscription.id,
            expected_version=99,
            reason="启用",
            idempotency_key="stale-not-ready",
        )
    assert stale_not_ready.value.code == "MONITOR_SUBSCRIPTION_VERSION_CONFLICT"

    ready, _, _, _ = service(ready=True)
    created = await ready.create(
        security_id=uuid4(), symbol="600001.SH", config=config(idempotency_key="other")
    )
    enabled = await ready.enable(
        created.subscription.id,
        expected_version=1,
        reason="启用",
        idempotency_key="enable",
    )
    assert enabled.subscription.status == SubscriptionStatus.ENABLED
    ready.targets.value = False
    replay = await ready.enable(
        created.subscription.id,
        expected_version=1,
        reason="启用",
        idempotency_key="enable",
    )
    assert replay.replayed is True
    assert replay.subscription.status == SubscriptionStatus.ENABLED
    paused = await ready.pause(
        created.subscription.id,
        expected_version=2,
        reason="暂停",
        idempotency_key="pause",
    )
    assert paused.subscription.status == SubscriptionStatus.PAUSED
    with pytest.raises(AppError) as stale:
        await ready.pause(
            created.subscription.id,
            expected_version=2,
            reason="重复暂停",
            idempotency_key="stale",
        )
    assert stale.value.code == "MONITOR_SUBSCRIPTION_VERSION_CONFLICT"


@pytest.mark.anyio
async def test_configure_appends_revision_and_freezes_new_schedule() -> None:
    svc, repo, _, _ = service()
    created = await svc.create(security_id=uuid4(), symbol="600000.SH", config=config())
    schedule_id = uuid4()
    schedule_revision_id = uuid4()
    changed = await svc.configure(
        created.subscription.id,
        config(
            schedule_id=schedule_id,
            schedule_revision_id=schedule_revision_id,
            expected_version=1,
            idempotency_key="configure",
            reason="更新调度",
        ),
    )
    assert changed.revision.id != created.revision.id
    assert changed.revision.revision_no == 2
    assert changed.revision.schedule_revision_id == schedule_revision_id
    assert changed.subscription.current_revision_id == changed.revision.id
    assert len(repo.revisions) == 2


@pytest.mark.anyio
async def test_archive_requires_paused_and_restore_is_always_paused() -> None:
    svc, _, _, _ = service()
    created = await svc.create(security_id=uuid4(), symbol="600000.SH", config=config())
    with pytest.raises(AppError) as invalid:
        await svc.archive(
            created.subscription.id,
            expected_version=1,
            reason="归档",
            idempotency_key="archive",
        )
    assert invalid.value.code == "MONITOR_SUBSCRIPTION_CONFLICT"
    await svc.pause(
        created.subscription.id,
        expected_version=1,
        reason="暂停",
        idempotency_key="pause",
    )
    archived = await svc.archive(
        created.subscription.id,
        expected_version=2,
        reason="归档",
        idempotency_key="archive",
    )
    assert archived.subscription.status == SubscriptionStatus.ARCHIVED
    restored = await svc.restore(
        created.subscription.id,
        expected_version=3,
        reason="恢复",
        idempotency_key="restore",
    )
    assert restored.subscription.status == SubscriptionStatus.PAUSED
    assert restored.subscription.archived_at is None


@pytest.mark.anyio
async def test_successful_request_replays_current_state_after_archive() -> None:
    svc, _, _, _ = service()
    created = await svc.create(security_id=uuid4(), symbol="600000.SH", config=config())
    await svc.pause(
        created.subscription.id,
        expected_version=1,
        reason="暂停",
        idempotency_key="pause-replay",
    )
    await svc.archive(
        created.subscription.id,
        expected_version=2,
        reason="归档",
        idempotency_key="archive-replay",
    )
    replay = await svc.pause(
        created.subscription.id,
        expected_version=1,
        reason="暂停",
        idempotency_key="pause-replay",
    )
    assert replay.replayed is True
    assert replay.subscription.status == SubscriptionStatus.ARCHIVED
    assert replay.subscription.version == 3
    with pytest.raises(AppError) as changed:
        await svc.pause(
            created.subscription.id,
            expected_version=1,
            reason="不同原因",
            idempotency_key="pause-replay",
        )
    assert changed.value.code == "MONITOR_SUBSCRIPTION_CONFLICT"


@pytest.mark.anyio
async def test_execute_if_eligible_runs_action_only_while_current() -> None:
    svc, _, _, _ = service(ready=True)
    created = await svc.create(security_id=uuid4(), symbol="600000.SH", config=config())
    enabled = await svc.enable(
        created.subscription.id,
        expected_version=1,
        reason="启用",
        idempotency_key="enable-fence",
    )
    calls = []

    async def action(session, snapshot):
        calls.append((session, snapshot))

    eligible = await svc.execute_if_eligible(
        enabled.subscription.id, frozen_version=2, action=action
    )
    assert eligible.status == "ELIGIBLE" and len(calls) == 1
    superseded = await svc.execute_if_eligible(
        enabled.subscription.id, frozen_version=1, action=action
    )
    assert superseded.status == "SUPERSEDED" and len(calls) == 1


@pytest.mark.anyio
async def test_pause_fence_supersedes_old_snapshot_without_side_effects() -> None:
    svc, _, _, events = service(ready=True)
    created = await svc.create(security_id=uuid4(), symbol="600000.SH", config=config())
    enabled = await svc.enable(
        created.subscription.id,
        expected_version=1,
        reason="启用",
        idempotency_key="enable",
    )
    snapshot = FrozenSubscription(
        subscription_id=enabled.subscription.id,
        security_id=enabled.subscription.security_id,
        symbol=enabled.subscription.symbol,
        version=enabled.subscription.version,
        revision_id=enabled.revision.id,
    )
    await svc.pause(
        enabled.subscription.id,
        expected_version=2,
        reason="暂停",
        idempotency_key="pause",
    )
    count = len(events.items)
    eligibility = await svc.final_eligibility(snapshot)
    assert eligibility.status == "SUPERSEDED"
    assert len(events.items) == count
