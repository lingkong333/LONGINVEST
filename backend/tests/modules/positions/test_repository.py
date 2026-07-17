from uuid import uuid4

import pytest

from long_invest.modules.positions.models import UserPosition, UserPositionHistory
from long_invest.modules.positions.repository import PositionRepository


@pytest.mark.anyio
async def test_missing_position_reads_as_none_without_inserting() -> None:
    class Session:
        added: list[object] = []

        async def scalar(self, _statement):
            return None

        def add(self, value):
            self.added.append(value)

    session = Session()
    repository = PositionRepository(session)

    assert await repository.get_current(uuid4()) is None
    assert session.added == []


@pytest.mark.anyio
async def test_new_position_is_flushed_before_its_history_pointer() -> None:
    class Session:
        def __init__(self):
            self.added = []
            self.snapshots = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            self.snapshots.append(
                tuple(getattr(value, "latest_history_id", None) for value in self.added)
            )

    security_id = uuid4()
    position_id = uuid4()
    history_id = uuid4()
    position = UserPosition(
        id=position_id,
        security_id=security_id,
        symbol="600000.SH",
        status="HOLDING",
        version=1,
        source="manual",
    )
    history = UserPositionHistory(
        id=history_id,
        position_id=position_id,
        security_id=security_id,
        symbol="600000.SH",
        before_status="NOT_HOLDING",
        after_status="HOLDING",
        effective_at="2026-07-17T09:00:00+00:00",
        source="manual",
        request_id="req",
        idempotency_key="idem",
        actor_user_id="user",
        position_version=1,
    )
    position.latest_history_id = history_id
    session = Session()

    await PositionRepository(session).add_change(position, history)

    assert session.snapshots[0] == (None,)
    assert history.position_id == position.id == position_id
    assert position.latest_history_id == history.id == history_id
