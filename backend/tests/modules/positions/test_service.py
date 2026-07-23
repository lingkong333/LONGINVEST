from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.positions.contracts import (
    PositionAction,
    PositionAuditContext,
    PositionStatus,
    SetPosition,
)
from long_invest.modules.positions.service import (
    PositionService,
    position_allowed_actions,
)
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)


def test_allowed_actions_follow_current_position() -> None:
    assert position_allowed_actions(PositionStatus.HOLDING) == (
        PositionAction.CLEAR,
    )
    assert position_allowed_actions(PositionStatus.NOT_HOLDING) == (
        PositionAction.HOLD,
    )


class Repository:
    def __init__(self):
        self.current = None
        self.histories = []

    async def lock_security(self, _security_id):
        return None

    async def lock_current(self, _security_id):
        return self.current

    async def find_history_by_idempotency(self, _security_id, key):
        return next(
            (item for item in self.histories if item.idempotency_key == key), None
        )

    async def add_change(self, position, history):
        self.current = position
        self.histories.append(history)

    async def get_current(self, _security_id):
        return self.current


class Audit:
    def __init__(self):
        self.records = []

    async def append(self, record):
        self.records.append(record)

    async def find_by_idempotency(self, key):
        return next(
            (item for item in self.records if item.idempotency_key == key), None
        )


class Events:
    def __init__(self):
        self.items = []

    async def append(self, event):
        self.items.append(event)


def command(target, *, key="idem-1", expected_version=None):
    return SetPosition(
        security_id=uuid4(),
        symbol="600000.SH",
        target=target,
        note="  长线持有  ",
        source="manual",
        request_id="req-1",
        idempotency_key=key,
        actor_user_id="user-1",
        expected_version=expected_version,
        audit_context=PositionAuditContext(
            request_id="req-1",
            idempotency_key=key,
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
            reason="确认持仓事实",
        ),
    )


@pytest.mark.anyio
async def test_default_not_holding_does_not_create_current_or_history() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    cmd = command(PositionStatus.NOT_HOLDING)

    result = await service.set(cmd)

    assert result.code == "POSITION_UNCHANGED"
    assert result.position.status is PositionStatus.NOT_HOLDING
    assert result.position.version == 0
    assert repository.current is None
    assert repository.histories == []
    assert events.items == []
    assert len(audit.records) == 1


@pytest.mark.anyio
async def test_default_get_keeps_the_resolved_symbol_without_writing() -> None:
    repository = Repository()
    service = PositionService(repository)

    result = await service.get(uuid4(), symbol="600000.SH")

    assert result.symbol == "600000.SH"
    assert result.status is PositionStatus.NOT_HOLDING
    assert repository.current is None


@pytest.mark.anyio
async def test_real_holding_change_appends_history_and_three_events() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    cmd = command(PositionStatus.HOLDING)

    result = await service.set(cmd)

    assert result.code == "POSITION_CHANGED"
    assert result.position.version == 1
    assert repository.current.id is not None
    assert repository.histories[0].id is not None
    assert repository.histories[0].position_id == repository.current.id
    assert repository.current.latest_history_id == repository.histories[0].id
    assert repository.histories[0].note == "长线持有"
    assert {item.event_type for item in events.items} == {
        "position.changed",
        "position.became_holding",
        "position.high_review_requested",
    }
    assert {item.dedupe_key for item in events.items} == {
        f"position:{cmd.security_id}:1:changed",
        f"position:{cmd.security_id}:1:became-holding",
        f"position:{cmd.security_id}:1:high-review",
    }
    assert audit.records[0].reason == "确认持仓事实"


@pytest.mark.anyio
async def test_same_state_has_only_lightweight_audit() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    first = command(PositionStatus.HOLDING, key="first")
    await service.set(first)
    replay = first.model_copy(
        update={
            "idempotency_key": "second",
            "audit_context": first.audit_context.model_copy(
                update={"idempotency_key": "second"}
            ),
        }
    )

    result = await service.set(replay)

    assert result.code == "POSITION_UNCHANGED"
    assert result.position.version == 1
    assert len(repository.histories) == 1
    assert len(events.items) == 3
    assert audit.records[-1].result == "UNCHANGED"


@pytest.mark.anyio
async def test_clear_emits_cancellation_events_and_increments_version() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    first = command(PositionStatus.HOLDING, key="first")
    await service.set(first)
    clear = first.model_copy(
        update={
            "target": PositionStatus.NOT_HOLDING,
            "idempotency_key": "clear",
            "audit_context": first.audit_context.model_copy(
                update={"idempotency_key": "clear"}
            ),
        }
    )

    result = await service.set(clear)

    assert result.position.version == 2
    assert {item.event_type for item in events.items[-3:]} == {
        "position.changed",
        "position.became_not_holding",
        "high_notification_cancel_requested",
    }


@pytest.mark.anyio
async def test_expected_version_conflict_rejects_change() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    repository.current = SimpleNamespace(status="HOLDING", version=2)
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )

    with pytest.raises(AppError) as caught:
        await service.set(command(PositionStatus.NOT_HOLDING, expected_version=1))

    assert caught.value.code == "POSITION_VERSION_CONFLICT"
    assert events.items == []


@pytest.mark.anyio
async def test_same_idempotency_key_with_different_target_conflicts() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    first = command(PositionStatus.HOLDING)
    await service.set(first)

    with pytest.raises(AppError) as caught:
        await service.set(
            first.model_copy(update={"target": PositionStatus.NOT_HOLDING})
        )

    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_same_idempotency_key_with_different_note_conflicts() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    first = command(PositionStatus.HOLDING)
    await service.set(first)

    with pytest.raises(AppError) as caught:
        await service.set(first.model_copy(update={"note": "不同原因"}))

    assert caught.value.code == "POSITION_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_same_idempotency_key_with_different_reason_conflicts() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    first = command(PositionStatus.HOLDING)
    await service.set(first)
    assert first.audit_context is not None
    changed_context = first.audit_context.model_copy(
        update={"reason": "另一个操作原因"}
    )

    with pytest.raises(AppError) as caught:
        await service.set(first.model_copy(update={"audit_context": changed_context}))

    assert caught.value.code == "POSITION_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_unchanged_request_replays_without_duplicate_audit() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    unchanged = command(PositionStatus.NOT_HOLDING)
    first = await service.set(unchanged)

    replay = await service.set(unchanged)

    assert first.code == replay.code == "POSITION_UNCHANGED"
    assert replay.replayed is True
    assert len(audit.records) == 1


@pytest.mark.anyio
async def test_unchanged_idempotency_key_cannot_later_change_target() -> None:
    repository, audit, events = Repository(), Audit(), Events()
    service = PositionService(
        repository, audit_service=audit, event_sink=events, now=lambda: NOW
    )
    unchanged = command(PositionStatus.NOT_HOLDING)
    await service.set(unchanged)

    with pytest.raises(AppError) as caught:
        await service.set(
            unchanged.model_copy(update={"target": PositionStatus.HOLDING})
        )

    assert caught.value.code == "POSITION_IDEMPOTENCY_CONFLICT"
