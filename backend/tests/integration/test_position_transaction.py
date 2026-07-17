from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.modules.positions.contracts import (
    PositionAuditContext,
    PositionStatus,
    SetPosition,
)
from long_invest.modules.positions.models import UserPosition, UserPositionHistory
from long_invest.modules.positions.repository import PositionRepository
from long_invest.modules.positions.service import PositionService
from long_invest.modules.securities.models import Security
from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.outbox.models import EventOutbox

pytestmark = pytest.mark.skipif(
    os.environ.get("LONGINVEST_POSITION_POSTGRES_TESTS") != "1",
    reason="requires migrated PostgreSQL profile",
)

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed(database: Database):
    token = uuid4().hex
    symbol = f"{int(token[:8], 16) % 1_000_000:06d}.SH"
    security = Security(
        id=uuid4(),
        symbol=symbol,
        exchange_code=symbol[:6],
        name=f"Position integration {token}",
        market="SH",
        security_type="A_SHARE",
        listing_status="LISTED",
        is_st=False,
        is_suspended=False,
        provider_codes={},
        master_version=1,
        source="integration-test",
        source_version=token,
        updated_at=NOW,
    )
    async with database.transaction() as session:
        session.add(security)
    return security


def _command(security, key: str) -> SetPosition:
    return SetPosition(
        security_id=security.id,
        symbol=security.symbol,
        target=PositionStatus.HOLDING,
        source="integration-test",
        request_id=f"req-{key}",
        idempotency_key=key,
        actor_user_id="integration-user",
        audit_context=PositionAuditContext(
            request_id=f"req-{key}",
            idempotency_key=key,
            actor_user_id="integration-user",
            session_id="integration-session",
            trusted_ip="127.0.0.1",
            reason="integration test",
        ),
    )


class FailingEvents:
    async def append(self, _event):
        raise RuntimeError("outbox failed")


class FailingAudit:
    async def find_by_idempotency(self, _key):
        return None

    async def append(self, _record):
        raise RuntimeError("audit failed")


@pytest.mark.anyio
async def test_first_change_persists_both_foreign_key_directions() -> None:
    from long_invest.modules.positions.outbox import PositionOutboxAdapter

    database = Database(AppSettings(_env_file=None).database_url)
    security = await _seed(database)
    try:
        async with database.transaction() as session:
            await PositionService(
                PositionRepository(session),
                audit_service=AuditService(session),
                event_sink=PositionOutboxAdapter(session),
                now=lambda: NOW,
            ).set(_command(security, uuid4().hex))

        async with database.session() as session:
            position = await session.scalar(
                select(UserPosition).where(UserPosition.security_id == security.id)
            )
            history = await session.scalar(
                select(UserPositionHistory).where(
                    UserPositionHistory.security_id == security.id
                )
            )
        assert position is not None and history is not None
        assert history.position_id == position.id
        assert position.latest_history_id == history.id
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_outbox_failure_rolls_back_current_history_and_audit() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    security = await _seed(database)
    try:
        with pytest.raises(RuntimeError, match="outbox failed"):
            async with database.transaction() as session:
                await PositionService(
                    PositionRepository(session),
                    audit_service=AuditService(session),
                    event_sink=FailingEvents(),
                    now=lambda: NOW,
                ).set(_command(security, uuid4().hex))

        async with database.session() as session:
            current_count = await session.scalar(
                select(func.count())
                .select_from(UserPosition)
                .where(UserPosition.security_id == security.id)
            )
            history_count = await session.scalar(
                select(func.count())
                .select_from(UserPositionHistory)
                .where(UserPositionHistory.security_id == security.id)
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.object_id == str(security.id))
            )
        assert (current_count, history_count, audit_count) == (0, 0, 0)
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_audit_failure_rolls_back_current_and_history() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    security = await _seed(database)
    try:
        with pytest.raises(RuntimeError, match="audit failed"):
            async with database.transaction() as session:
                await PositionService(
                    PositionRepository(session),
                    audit_service=FailingAudit(),
                    event_sink=FailingEvents(),
                    now=lambda: NOW,
                ).set(_command(security, uuid4().hex))

        async with database.session() as session:
            current_count = await session.scalar(
                select(func.count())
                .select_from(UserPosition)
                .where(UserPosition.security_id == security.id)
            )
            history_count = await session.scalar(
                select(func.count())
                .select_from(UserPositionHistory)
                .where(UserPositionHistory.security_id == security.id)
            )
        assert (current_count, history_count) == (0, 0)
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_concurrent_same_state_creates_one_history_and_three_events() -> None:
    from long_invest.modules.positions.outbox import PositionOutboxAdapter

    database = Database(AppSettings(_env_file=None).database_url)
    security = await _seed(database)

    async def hold(key: str):
        async with database.transaction() as session:
            return await PositionService(
                PositionRepository(session),
                audit_service=AuditService(session),
                event_sink=PositionOutboxAdapter(session),
                now=lambda: NOW,
            ).set(_command(security, key))

    try:
        results = await asyncio.gather(hold(uuid4().hex), hold(uuid4().hex))
        async with database.session() as session:
            history_count = await session.scalar(
                select(func.count())
                .select_from(UserPositionHistory)
                .where(UserPositionHistory.security_id == security.id)
            )
            event_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.aggregate_id == str(security.id))
            )
        assert {item.code for item in results} == {
            "POSITION_CHANGED",
            "POSITION_UNCHANGED",
        }
        assert history_count == 1
        assert event_count == 3
    finally:
        await database.dispose()
