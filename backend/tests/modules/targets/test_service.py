from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.targets.contracts import (
    ManualTargetCommand,
    RestoreTargetCommand,
    TargetSource,
    TargetValues,
)
from long_invest.modules.targets.service import TargetService
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)


class Repository:
    def __init__(self):
        self.binding = SimpleNamespace(
            subscription_id=SUBSCRIPTION_ID,
            current_revision_id=None,
            status="MISSING",
            version=1,
            activated_at=None,
            stale_reason=None,
        )
        self.revisions = []

    async def lock_binding(self, _subscription_id):
        return self.binding

    async def create_binding(self, subscription_id):
        self.binding.subscription_id = subscription_id
        return self.binding

    async def find_revision_by_idempotency(self, _subscription_id, key):
        return next((row for row in self.revisions if row.idempotency_key == key), None)

    async def get_revision(self, revision_id):
        return next((row for row in self.revisions if row.id == revision_id), None)

    async def list_revisions(self, _subscription_id):
        return tuple(reversed(self.revisions))

    async def persist_revision(self, revision):
        self.revisions.append(revision)

    async def flush(self):
        return None


class Subscriptions:
    def __init__(self, *, status="ENABLED", target_mode="MANUAL"):
        self.snapshot = SimpleNamespace(
            subscription_id=SUBSCRIPTION_ID,
            status=status,
            target_mode=target_mode,
            version=4,
        )
        self.switches = []

    async def lock(self, _subscription_id):
        return self.snapshot

    async def switch_to_manual(self, **kwargs):
        self.switches.append(kwargs)
        self.snapshot.target_mode = "MANUAL"
        self.snapshot.version += 1
        return self.snapshot


class Sink:
    def __init__(self):
        self.items = []

    async def append(self, item):
        self.items.append(item)


SUBSCRIPTION_ID = uuid4()


def values(raw=("8", "9", "12", "13")):
    return TargetValues(
        low_strong=raw[0], low_watch=raw[1], high_watch=raw[2], high_strong=raw[3]
    )


def manual(*, key="manual-1", target_values=None, expected_version=1, **updates):
    data = dict(
        subscription_id=SUBSCRIPTION_ID,
        target_date=date(2026, 7, 17),
        values=target_values or values(),
        reason="manual target",
        expected_version=expected_version,
        idempotency_key=key,
        request_id="req-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )
    data.update(updates)
    return ManualTargetCommand(**data)


def service(repository=None, subscriptions=None):
    repository = repository or Repository()
    audit, events = Sink(), Sink()
    return (
        TargetService(
            repository,
            subscriptions=subscriptions or Subscriptions(),
            audit=audit,
            events=events,
            now=lambda: NOW,
        ),
        repository,
        audit,
        events,
    )


@pytest.mark.anyio
async def test_manual_target_creates_revision_binding_audit_and_reevaluation() -> None:
    target, repository, audit, events = service()

    result = await target.set_manual(manual())

    assert result.binding.status.value == "READY"
    assert result.binding.version == 2
    assert result.revision.source is TargetSource.MANUAL
    assert [item.event_type for item in events.items] == [
        "target.activated",
        "signal.reevaluation_requested",
    ]
    assert [item.action_code for item in audit.items] == ["target.manual_activated"]
    assert repository.binding.current_revision_id == result.revision.id


@pytest.mark.anyio
async def test_large_manual_change_requires_second_confirmation() -> None:
    target, repository, *_ = service()
    first = await target.set_manual(manual(key="first"))

    with pytest.raises(AppError) as caught:
        await target.set_manual(
            manual(
                key="second",
                expected_version=first.binding.version,
                target_values=values(("4", "5", "14", "15")),
            )
        )

    assert caught.value.code == "TARGET_CONFIRMATION_REQUIRED"
    assert len(repository.revisions) == 1


@pytest.mark.anyio
async def test_idempotency_replay_precedes_stale_expected_version() -> None:
    target, repository, audit, events = service()
    command = manual()
    first = await target.set_manual(command)

    replay = await target.set_manual(command)

    assert replay.replayed is True
    assert replay.revision.id == first.revision.id
    assert len(repository.revisions) == len(audit.items) == 1
    assert len(events.items) == 2


@pytest.mark.anyio
async def test_old_idempotency_replay_returns_its_original_binding_snapshot() -> None:
    target, *_ = service()
    first_command = manual(key="first")
    first = await target.set_manual(first_command)
    await target.set_manual(
        manual(
            key="second",
            expected_version=first.binding.version,
            target_values=values(("8.50", "9.50", "12.50", "13.50")),
        )
    )

    replay = await target.set_manual(first_command)

    assert replay.binding.current_revision_id == first.revision.id
    assert replay.binding.version == first.binding.version


@pytest.mark.anyio
async def test_same_key_with_different_content_conflicts() -> None:
    target, *_ = service()
    await target.set_manual(manual())

    with pytest.raises(AppError) as caught:
        await target.set_manual(manual(target_values=values(("7", "9", "12", "13"))))

    assert caught.value.code == "TARGET_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_expected_binding_version_conflicts() -> None:
    target, *_ = service()

    with pytest.raises(AppError) as caught:
        await target.set_manual(manual(expected_version=9))

    assert caught.value.code == "TARGET_VERSION_CONFLICT"


@pytest.mark.anyio
async def test_archived_subscription_is_rejected() -> None:
    target, *_ = service(subscriptions=Subscriptions(status="ARCHIVED"))

    with pytest.raises(AppError) as caught:
        await target.set_manual(manual())

    assert caught.value.code == "TARGET_SUBSCRIPTION_ARCHIVED"


@pytest.mark.anyio
async def test_strategy_mode_requires_confirmation_then_switches() -> None:
    subscriptions = Subscriptions(target_mode="STRATEGY")
    target, *_ = service(subscriptions=subscriptions)

    with pytest.raises(AppError) as caught:
        await target.set_manual(manual())
    assert caught.value.code == "TARGET_MODE_SWITCH_CONFIRMATION_REQUIRED"

    result = await target.set_manual(
        manual(key="confirmed", switch_to_manual_confirmed=True)
    )
    assert result.code == "TARGET_MANUAL_ACTIVATED"
    assert len(subscriptions.switches) == 1


@pytest.mark.anyio
async def test_restore_copies_historical_values_into_new_revision() -> None:
    target, repository, *_ = service()
    original = await target.set_manual(manual(key="original"))
    await target.set_manual(
        manual(
            key="new",
            expected_version=original.binding.version,
            target_values=values(("10", "11", "14", "15")),
            large_change_confirmed=True,
        )
    )
    restored = await target.restore(
        RestoreTargetCommand(
            subscription_id=SUBSCRIPTION_ID,
            source_revision_id=original.revision.id,
            reason="restore old values",
            expected_version=3,
            idempotency_key="restore",
            request_id="req-restore",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
            switch_to_manual_confirmed=True,
        )
    )

    assert restored.revision.source is TargetSource.RESTORED
    assert restored.revision.source_revision_id == original.revision.id
    assert restored.revision.id != original.revision.id
    assert restored.revision.values == original.revision.values
    assert len(repository.revisions) == 3


@pytest.mark.anyio
async def test_restore_stale_binding_has_specific_conflict_code() -> None:
    target, *_ = service()
    original = await target.set_manual(manual(key="original"))

    with pytest.raises(AppError) as caught:
        await target.restore(
            RestoreTargetCommand(
                subscription_id=SUBSCRIPTION_ID,
                source_revision_id=original.revision.id,
                reason="stale restore",
                expected_version=1,
                idempotency_key="restore-stale",
                request_id="req-restore",
                actor_user_id="user-1",
                session_id="session-1",
                trusted_ip="127.0.0.1",
                switch_to_manual_confirmed=True,
            )
        )

    assert caught.value.code == "TARGET_RESTORE_STALE"
