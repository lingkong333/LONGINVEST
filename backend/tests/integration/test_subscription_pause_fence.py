from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.modules.monitoring.application import (
    MonitorSubscriptionApplication,
    SubscriptionAudit,
)
from long_invest.modules.monitoring.models import (
    MonitorSubscription,
    MonitorSubscriptionRevision,
)
from long_invest.modules.monitoring.outbox import MonitorSubscriptionOutbox
from long_invest.modules.monitoring.repository import MonitorSubscriptionRepository
from long_invest.modules.monitoring.service import (
    MonitorSubscriptionService,
    SubscriptionConfig,
)
from long_invest.modules.securities.models import Security
from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.outbox.models import EventOutbox
from long_invest.platform.outbox.service import TransactionalOutboxWriter

pytestmark = pytest.mark.skipif(
    os.environ.get("LONGINVEST_MONITORING_POSTGRES_TESTS") != "1",
    reason="requires migrated PostgreSQL profile",
)
NOW = datetime(2026, 7, 17, 10, tzinfo=UTC)


class Ready:
    async def current_readiness(self, subscription_id):
        return True

    async def published_version(self, strategy_version_id):
        return True


class FailingEvents:
    async def publish(self, event):
        raise RuntimeError("outbox failed")


class FailingAudit:
    async def find_replay(self, **kwargs):
        return None

    async def record(self, event):
        raise RuntimeError("audit failed")


async def _seed(database):
    token = uuid4().hex
    symbol = f"{int(token[:8], 16) % 1_000_000:06d}.SH"
    security = Security(
        id=uuid4(),
        symbol=symbol,
        exchange_code=symbol[:6],
        name=f"Monitoring {token}",
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


def _config(key):
    return SubscriptionConfig(reason="integration test", idempotency_key=key)


def _service(session, *, audit=None, events=None):
    return MonitorSubscriptionService(
        MonitorSubscriptionRepository(session),
        audit=audit or SubscriptionAudit(session),
        events=events or MonitorSubscriptionOutbox(session),
        target_readiness=Ready(),
        strategy_readiness=Ready(),
        now=lambda: NOW,
    )


@pytest.mark.anyio
async def test_real_postgres_circular_pointer_and_unique_concurrent_create() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    security = await _seed(database)

    async def create(key):
        async with database.transaction() as session:
            return await _service(session).create(
                security_id=security.id, symbol=security.symbol, config=_config(key)
            )

    try:
        first, second = await asyncio.gather(create(uuid4().hex), create(uuid4().hex))
        async with database.session() as session:
            owners = (
                await session.scalars(
                    select(MonitorSubscription).where(
                        MonitorSubscription.security_id == security.id
                    )
                )
            ).all()
            revisions = (
                await session.scalars(
                    select(MonitorSubscriptionRevision).where(
                        MonitorSubscriptionRevision.subscription_id == owners[0].id
                    )
                )
            ).all()
        assert first.subscription.id == second.subscription.id == owners[0].id
        assert len(owners) == len(revisions) == 1
        assert owners[0].current_revision_id == revisions[0].id
    finally:
        await database.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["audit", "outbox"])
async def test_real_postgres_audit_and_outbox_failure_roll_back(failure) -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    security = await _seed(database)
    try:
        async with database.session() as session:
            audit_before = await session.scalar(
                select(func.count()).select_from(AuditEvent)
            )
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            async with database.transaction() as session:
                await _service(
                    session,
                    audit=FailingAudit() if failure == "audit" else None,
                    events=FailingEvents() if failure == "outbox" else None,
                ).create(
                    security_id=security.id,
                    symbol=security.symbol,
                    config=_config(uuid4().hex),
                )
        async with database.session() as session:
            owner_count = await session.scalar(
                select(func.count())
                .select_from(MonitorSubscription)
                .where(MonitorSubscription.security_id == security.id)
            )
            revision_count = await session.scalar(
                select(func.count())
                .select_from(MonitorSubscriptionRevision)
                .join(
                    MonitorSubscription,
                    MonitorSubscription.id
                    == MonitorSubscriptionRevision.subscription_id,
                )
                .where(MonitorSubscription.security_id == security.id)
            )
            audit_after = await session.scalar(
                select(func.count()).select_from(AuditEvent)
            )
        assert (owner_count, revision_count) == (0, 0)
        assert audit_after == audit_before
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_real_postgres_subscription_pause_fence() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    security = await _seed(database)
    try:
        async with database.transaction() as session:
            created = await _service(session).create(
                security_id=security.id,
                symbol=security.symbol,
                config=_config(uuid4().hex),
            )
        async with database.transaction() as session:
            enabled = await _service(session).enable(
                created.subscription.id,
                expected_version=1,
                reason="enable",
                idempotency_key=uuid4().hex,
            )
        app = MonitorSubscriptionApplication(
            database,
            security_application=object(),
            schedule_application=object(),
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def action(session, snapshot):
            entered.set()
            await release.wait()
            await TransactionalOutboxWriter().append(
                session=session,
                topic="signal.fence-test",
                aggregate_type="monitor_subscription",
                aggregate_id=str(snapshot.subscription_id),
                queue="domain-events",
                payload={"subscription_id": str(snapshot.subscription_id)},
                dedupe_key=f"signal-fence:{snapshot.subscription_id}:{snapshot.version}",
            )

        eligibility_task = asyncio.create_task(
            app.execute_if_eligible(
                enabled.subscription.id, frozen_version=2, action=action
            )
        )
        await entered.wait()

        async def pause():
            async with database.transaction() as session:
                return await _service(session).pause(
                    enabled.subscription.id,
                    expected_version=2,
                    reason="pause",
                    idempotency_key=uuid4().hex,
                )

        pause_task = asyncio.create_task(pause())
        await asyncio.sleep(0.2)
        assert not pause_task.done()
        release.set()
        eligible, _paused = await asyncio.gather(eligibility_task, pause_task)
        assert eligible.status == "ELIGIBLE"

        called = False

        async def forbidden_action(session, snapshot):
            nonlocal called
            called = True

        superseded = await app.execute_if_eligible(
            enabled.subscription.id, frozen_version=2, action=forbidden_action
        )
        async with database.session() as session:
            forbidden_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(
                    EventOutbox.aggregate_id == str(enabled.subscription.id),
                    EventOutbox.topic.in_(
                        ("signal.forbidden", "notification.forbidden")
                    ),
                )
            )
        assert superseded.status == "SUPERSEDED"
        assert called is False
        assert forbidden_count == 0
    finally:
        await database.dispose()
