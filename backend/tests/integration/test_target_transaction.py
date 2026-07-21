import hashlib
import os
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.modules.monitor_schedules.models import MonitorSchedule  # noqa: F401
from long_invest.modules.monitoring.models import (
    MonitorSubscription,
    MonitorSubscriptionRevision,
)
from long_invest.modules.securities.models import Security
from long_invest.modules.targets.application import TargetApplication
from long_invest.modules.targets.contracts import ManualTargetCommand, TargetValues
from long_invest.modules.targets.models import SubscriptionTargetBinding, TargetRevision
from long_invest.modules.targets.outbox import TargetOutbox
from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.outbox.models import EventOutbox

pytestmark = pytest.mark.skipif(
    os.environ.get("LONGINVEST_TARGET_POSTGRES_TESTS") != "1",
    reason="requires migrated PostgreSQL profile",
)

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class SubscriptionPort:
    def __init__(self, session, *, fail=False):
        self.session = session
        self.fail = fail
        self.owner = None
        self.revision = None

    async def lock(self, subscription_id):
        self.owner = await self.session.scalar(
            select(MonitorSubscription)
            .where(MonitorSubscription.id == subscription_id)
            .with_for_update()
        )
        if self.owner is None:
            return None
        self.revision = await self.session.get(
            MonitorSubscriptionRevision, self.owner.current_revision_id
        )
        return SimpleNamespace(
            subscription_id=self.owner.id,
            status=self.owner.status,
            target_mode=self.revision.target_mode,
            version=self.owner.version,
        )

    async def switch_to_manual(self, **kwargs):
        revision = MonitorSubscriptionRevision(
            id=uuid4(),
            subscription_id=self.owner.id,
            revision_no=self.revision.revision_no + 1,
            schedule_id=self.revision.schedule_id,
            schedule_revision_id=self.revision.schedule_revision_id,
            target_mode="MANUAL",
            target_version_id=None,
            strategy_version_id=None,
            parameters=dict(self.revision.parameters),
            hysteresis_ratio=self.revision.hysteresis_ratio,
            hysteresis_min=self.revision.hysteresis_min,
            notification_mode=self.revision.notification_mode,
            reason=kwargs["reason"],
            created_by_user_id=kwargs["actor_user_id"],
            request_id=kwargs["request_id"],
            idempotency_key=kwargs["idempotency_key"],
            content_hash=hashlib.sha256(kwargs["idempotency_key"].encode()).hexdigest(),
            created_at=NOW,
        )
        self.session.add(revision)
        self.owner.current_revision_id = revision.id
        self.owner.version += 1
        await self.session.flush()
        if self.fail:
            raise RuntimeError("mode switch failed")
        return SimpleNamespace(target_mode="MANUAL", version=self.owner.version)


class FailingAudit:
    async def append(self, _record):
        raise RuntimeError("audit failed")


class FailingEvents:
    async def append(self, _event):
        raise RuntimeError("outbox failed")


async def _seed(database):
    token = uuid4().hex
    security = Security(
        id=uuid4(),
        symbol=f"{int(token[:8], 16) % 1_000_000:06d}.SH",
        exchange_code=token[:6],
        name=f"Target integration {token}",
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
    owner = MonitorSubscription(
        id=uuid4(),
        security_id=security.id,
        symbol=security.symbol,
        status="ENABLED",
        current_revision_id=None,
        version=1,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    revision = MonitorSubscriptionRevision(
        id=uuid4(),
        subscription_id=owner.id,
        revision_no=1,
        schedule_id=None,
        schedule_revision_id=None,
        target_mode="STRATEGY",
        target_version_id=None,
        strategy_version_id=uuid4(),
        parameters={},
        hysteresis_ratio="0.02",
        hysteresis_min="0.02",
        notification_mode="ALL",
        reason="seed",
        created_by_user_id="integration-user",
        request_id=f"seed-{token}",
        idempotency_key=f"seed-{token}",
        content_hash=hashlib.sha256(token.encode()).hexdigest(),
        created_at=NOW,
    )
    async with database.transaction() as session:
        session.add(security)
        await session.flush()
        session.add(owner)
        await session.flush()
        session.add(revision)
        await session.flush()
        owner.current_revision_id = revision.id
    return owner, revision


def _command(subscription_id, key):
    return ManualTargetCommand(
        subscription_id=subscription_id,
        target_date=date(2026, 7, 17),
        values=TargetValues(
            low_strong="8", low_watch="9", high_watch="12", high_strong="13"
        ),
        reason="integration rollback",
        expected_version=1,
        idempotency_key=key,
        request_id=f"req-{key}",
        actor_user_id="integration-user",
        session_id="integration-session",
        trusted_ip="127.0.0.1",
        switch_to_manual_confirmed=True,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("audit", "audit failed"),
        ("outbox", "outbox failed"),
        ("switch", "mode switch failed"),
    ],
)
async def test_target_write_failure_rolls_back_every_module(failure, message) -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    owner, original_revision = await _seed(database)
    key = uuid4().hex
    application = TargetApplication(
        database,
        subscription_factory=lambda session: SubscriptionPort(
            session, fail=failure == "switch"
        ),
        audit_factory=(lambda _session: FailingAudit())
        if failure == "audit"
        else AuditService,
        event_factory=(lambda _session: FailingEvents())
        if failure == "outbox"
        else TargetOutbox,
    )
    try:
        with pytest.raises(RuntimeError, match=message):
            await application.set_manual(_command(owner.id, key))

        async with database.session() as session:
            target_revisions = await session.scalar(
                select(func.count())
                .select_from(TargetRevision)
                .where(TargetRevision.subscription_id == owner.id)
            )
            bindings = await session.scalar(
                select(func.count())
                .select_from(SubscriptionTargetBinding)
                .where(SubscriptionTargetBinding.subscription_id == owner.id)
            )
            subscription_revisions = await session.scalar(
                select(func.count())
                .select_from(MonitorSubscriptionRevision)
                .where(MonitorSubscriptionRevision.subscription_id == owner.id)
            )
            stored_owner = await session.get(MonitorSubscription, owner.id)
            audits = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.request_id == f"req-{key}")
            )
            events = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.aggregate_id == str(owner.id))
            )
        assert (target_revisions, bindings, audits, events) == (0, 0, 0, 0)
        assert subscription_revisions == 1
        assert stored_owner.version == 1
        assert stored_owner.current_revision_id == original_revision.id
    finally:
        await database.dispose()
