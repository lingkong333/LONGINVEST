from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.modules.auth.models import AppUser
from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityIdentity,
    SecurityType,
)
from long_invest.modules.securities.models import Security
from long_invest.modules.watchlists.application import WatchlistAuditContext
from long_invest.modules.watchlists.contracts import WatchlistMutation
from long_invest.modules.watchlists.models import Watchlist, WatchlistItem
from long_invest.modules.watchlists.outbox import WatchlistEventAdapter
from long_invest.modules.watchlists.repository import WatchlistRepository
from long_invest.modules.watchlists.service import WatchlistService
from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.outbox.models import EventOutbox
from long_invest.platform.outbox.service import TransactionalOutboxWriter

pytestmark = pytest.mark.skipif(
    os.environ.get("LONGINVEST_WATCHLIST_POSTGRES_TESTS") != "1",
    reason="requires migrated PostgreSQL profile",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed(database: Database) -> tuple[AppUser, Security, SecurityIdentity]:
    now = datetime.now(UTC)
    token = uuid4().hex
    symbol = f"{int(token[:8], 16) % 1_000_000:06d}.SH"
    async with database.transaction() as session:
        user = await session.scalar(select(AppUser).limit(1))
        if user is None:
            user = AppUser(
                id=uuid4(),
                username=f"watchlist-{token}",
                password_hash="integration-test",
                password_changed_at=now,
            )
            session.add(user)
        security = Security(
            id=uuid4(),
            symbol=symbol,
            exchange_code=symbol[:6],
            name=f"Watchlist integration {token}",
            market="SH",
            security_type="A_SHARE",
            listing_status="LISTED",
            is_st=False,
            is_suspended=False,
            provider_codes={},
            master_version=1,
            source="integration-test",
            source_version=token,
            updated_at=now,
        )
        session.add(security)
    identity = SecurityIdentity(
        security_id=security.id,
        symbol=security.symbol,
        market=Market.SH,
        security_type=SecurityType.A_SHARE,
        listing_status=ListingStatus.LISTED,
        is_suspended=False,
        is_st=False,
        listed_on=None,
        delisted_on=None,
        master_version=1,
    )
    return user, security, identity


def _context(user: AppUser, key: str) -> WatchlistAuditContext:
    return WatchlistAuditContext(
        request_id=f"req-{key}"[:64],
        actor_user_id=str(user.id),
        session_id="integration-session",
        trusted_ip="127.0.0.1",
    )


class AppendThenFailWriter:
    def __init__(self) -> None:
        self._writer = TransactionalOutboxWriter()

    async def append(self, **values) -> None:
        await self._writer.append(**values)
        raise RuntimeError("outbox failed after append")


class AppendThenFailAudit:
    def __init__(self, session) -> None:
        self._audit = AuditService(session)

    async def append(self, event) -> None:
        await self._audit.append(event)
        raise RuntimeError("audit failed after append")


@pytest.mark.anyio
async def test_business_audit_and_outbox_roll_back_together() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    user, _security, _identity = await _seed(database)
    token = uuid4().hex
    name = f"rollback-{token}"
    reason = f"rollback-reason-{token}"
    key = f"rollback-{token}"
    try:
        with pytest.raises(RuntimeError, match="outbox failed after append"):
            async with database.transaction() as session:
                await WatchlistService(
                    WatchlistRepository(session),
                    AuditService(session),
                    WatchlistEventAdapter(session, AppendThenFailWriter()),
                ).create(
                    user.id,
                    WatchlistMutation(
                        name=name,
                        description=None,
                        display_order=0,
                        reason=reason,
                        idempotency_key=key,
                    ),
                    audit_context=_context(user, key),
                )

        async with database.session() as session:
            business_count = await session.scalar(
                select(func.count())
                .select_from(Watchlist)
                .where(Watchlist.name == name)
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.reason == reason)
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.payload["reason"].astext == reason)
            )
        assert (business_count, audit_count, outbox_count) == (0, 0, 0)
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_audit_insert_failure_rolls_back_business_audit_and_outbox() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    user, _security, _identity = await _seed(database)
    token = uuid4().hex
    name = f"audit-rollback-{token}"
    reason = f"audit-rollback-reason-{token}"
    key = f"audit-rollback-{token}"
    try:
        with pytest.raises(RuntimeError, match="audit failed after append"):
            async with database.transaction() as session:
                await WatchlistService(
                    WatchlistRepository(session),
                    AppendThenFailAudit(session),
                    WatchlistEventAdapter(session),
                ).create(
                    user.id,
                    WatchlistMutation(
                        name=name,
                        description=None,
                        display_order=0,
                        reason=reason,
                        idempotency_key=key,
                    ),
                    audit_context=_context(user, key),
                )

        async with database.session() as session:
            business_count = await session.scalar(
                select(func.count())
                .select_from(Watchlist)
                .where(Watchlist.name == name)
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.reason == reason)
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.payload["reason"].astext == reason)
            )
        assert (business_count, audit_count, outbox_count) == (0, 0, 0)
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_concurrent_cross_group_removals_recommend_pause_once() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    user, security, identity = await _seed(database)
    token = uuid4().hex
    groups = []
    try:
        for index in range(2):
            async with database.transaction() as session:
                service = WatchlistService(
                    WatchlistRepository(session),
                    AuditService(session),
                    WatchlistEventAdapter(session),
                )
                group = await service.create(
                    user.id,
                    WatchlistMutation(
                        name=f"concurrent-{token}-{index}",
                        description=None,
                        display_order=index,
                        reason="并发移除测试",
                        idempotency_key=f"create-{token}-{index}",
                    ),
                    audit_context=_context(user, f"create-{token}-{index}"),
                )
                await service.add_item(
                    group.id,
                    owner_user_id=user.id,
                    security=identity,
                    source="integration-test",
                    reason="并发移除测试",
                    idempotency_key=f"add-{token}-{index}",
                    expected_version=1,
                    audit_context=_context(user, f"add-{token}-{index}"),
                )
                groups.append(group.id)

        async def remove(group_id, index):
            key = f"remove-{token}-{index}"
            async with database.transaction() as session:
                return await WatchlistService(
                    WatchlistRepository(session),
                    AuditService(session),
                    WatchlistEventAdapter(session),
                ).remove_item(
                    group_id,
                    owner_user_id=user.id,
                    security_id=security.id,
                    symbol=security.symbol,
                    reason="并发移除测试",
                    idempotency_key=key,
                    expected_version=2,
                    audit_context=_context(user, key),
                )

        results = await asyncio.gather(
            remove(groups[0], 0),
            remove(groups[1], 1),
        )
        async with database.session() as session:
            membership_count = await session.scalar(
                select(func.count())
                .select_from(WatchlistItem)
                .where(WatchlistItem.security_id == security.id)
            )
        assert membership_count == 0
        assert sorted(item.pause_recommended for item in results) == [False, True]
    finally:
        await database.dispose()
