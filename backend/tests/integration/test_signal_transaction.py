from __future__ import annotations

import asyncio
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
from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
    NotificationEventStatus,
)
from long_invest.modules.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
)
from long_invest.modules.notifications.service import (
    ChannelDeliveryTarget,
)
from long_invest.modules.quotes.models import QuoteCycle, QuoteCycleItem
from long_invest.modules.securities.models import Security
from long_invest.modules.signals.application import SignalApplication
from long_invest.modules.signals.contracts import (
    EvaluationReason,
    EvaluationResult,
    SignalInput,
)
from long_invest.modules.signals.integrations import TransactionalNotificationPublisher
from long_invest.modules.signals.models import (
    SignalEvaluation,
    SignalEvent,
    SignalState,
)
from long_invest.modules.signals.repository import SignalRepository
from long_invest.modules.targets.contracts import TargetValues
from long_invest.modules.targets.models import (
    SubscriptionTargetBinding,
    TargetRevision,
)
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.outbox.models import EventOutbox

pytestmark = pytest.mark.skipif(
    os.getenv("LONGINVEST_SIGNAL_TRANSACTION_TESTS") != "1",
    reason="set LONGINVEST_SIGNAL_TRANSACTION_TESTS=1 for PostgreSQL transaction tests",
)

NOW = datetime(2026, 7, 17, 9, 30, tzinfo=UTC)
TARGET_DATE = date(2026, 7, 17)
TARGETS = TargetValues(
    low_strong="8",
    low_watch="9",
    high_watch="11",
    high_strong="12",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _EmailTargetResolver:
    async def resolve_targets(self, notification):
        return (
            ChannelDeliveryTarget(
                DeliveryChannel.EMAIL,
                1,
                f"integration:{notification.subscription_id}",
            ),
        )


class _FirstIdempotencyReadBarrier:
    def __init__(self, participants: int) -> None:
        self._participants = participants
        self._arrived = 0
        self._ready = asyncio.Event()

    async def arrive(self) -> None:
        self._arrived += 1
        if self._arrived == self._participants:
            self._ready.set()
        await asyncio.wait_for(self._ready.wait(), timeout=5)


class _BarrierSignalRepository(SignalRepository):
    def __init__(self, session, barrier: _FirstIdempotencyReadBarrier) -> None:
        super().__init__(session)
        self._barrier = barrier
        self._first_idempotency_read = True

    async def find_evaluation_by_idempotency(
        self, subscription_id, idempotency_key
    ):
        evaluation = await super().find_evaluation_by_idempotency(
            subscription_id,
            idempotency_key,
        )
        if self._first_idempotency_read:
            self._first_idempotency_read = False
            await self._barrier.arrive()
        return evaluation


class _FailingDatabaseNotificationPublisher:
    def __init__(self, session, failure: str) -> None:
        self._session = session
        self._failure = failure

    async def publish(self, notification):
        if self._failure == "notification_fact":
            self._session.add(
                NotificationEvent(
                    event_type=None,
                    business_event_type="signal.transitioned",
                    business_event_id=str(notification.event_id),
                    business_object_type="monitor_subscription",
                    business_object_id=str(notification.subscription_id),
                    severity=None,
                    template_variables={},
                    status=NotificationEventStatus.ELIGIBLE,
                    eligibility_status=NotificationEventStatus.ELIGIBLE,
                    suppression_reason=None,
                    effective_channels=[],
                    template_version="v1",
                    idempotency_key=notification.idempotency_key,
                    content_hash="f" * 64,
                    request_id=notification.request_id,
                )
            )
        else:
            self._session.add(
                EventOutbox(
                    topic=None,
                    aggregate_type="signal",
                    aggregate_id=str(notification.subscription_id),
                    queue="notifications",
                    payload={},
                    dedupe_key=f"signal-notification:{notification.event_id}",
                )
            )
        await self._session.flush()


async def _seed(database: Database):
    token = uuid4().hex
    symbol = f"{int(token[:8], 16) % 1_000_000:06d}.SH"
    security = Security(
        id=uuid4(),
        symbol=symbol,
        exchange_code=symbol[:6],
        name=f"Signal integration {token}",
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
    subscription = MonitorSubscription(
        id=uuid4(),
        security_id=security.id,
        symbol=symbol,
        status="ENABLED",
        current_revision_id=None,
        version=1,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    subscription_revision = MonitorSubscriptionRevision(
        id=uuid4(),
        subscription_id=subscription.id,
        revision_no=1,
        schedule_id=None,
        schedule_revision_id=None,
        target_mode="MANUAL",
        target_version_id=None,
        strategy_version_id=None,
        parameters={},
        hysteresis_ratio="0.02",
        hysteresis_min="0.02",
        notification_mode="ALL",
        reason="integration seed",
        created_by_user_id="integration-user",
        request_id=f"seed-{token}",
        idempotency_key=f"seed-{token}",
        content_hash=hashlib.sha256(token.encode()).hexdigest(),
        created_at=NOW,
    )
    target_revision = TargetRevision(
        id=uuid4(),
        subscription_id=subscription.id,
        revision_no=1,
        low_strong=TARGETS.low_strong,
        low_watch=TARGETS.low_watch,
        high_watch=TARGETS.high_watch,
        high_strong=TARGETS.high_strong,
        source="MANUAL",
        source_revision_id=None,
        target_date=TARGET_DATE,
        strategy_version_id=None,
        parameter_snapshot={},
        data_version=None,
        source_code_hash=None,
        content_hash=hashlib.sha256(f"target-{token}".encode()).hexdigest(),
        reason="integration seed",
        large_change_confirmed=False,
        request_id=f"target-{token}",
        idempotency_key=f"target-{token}",
        actor_user_id="integration-user",
        session_id="integration-session",
        trusted_ip="127.0.0.1",
        created_at=NOW,
    )
    target_binding = SubscriptionTargetBinding(
        id=uuid4(),
        subscription_id=subscription.id,
        current_revision_id=target_revision.id,
        status="READY",
        version=1,
        activated_at=NOW,
        stale_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )
    quote_cycle = QuoteCycle(
        id=uuid4(),
        status="READY",
        scheduled_at=NOW,
        finalized_at=NOW,
        universe_snapshot_id=f"integration-{token}",
        universe_snapshot_version=1,
        subscription_snapshot_version=subscription.version,
        idempotency_scope="signal-integration",
        idempotency_key=token,
        expected_count=1,
        timeout_seconds=30,
        valid_count=1,
        missing_count=0,
        conflict_count=0,
        failed_count=0,
    )
    quote_item = QuoteCycleItem(
        id=uuid4(),
        cycle_id=quote_cycle.id,
        symbol=symbol,
        expected_subscription_version=subscription.version,
        status="VALID",
        price="8.50",
        quote_time=NOW,
        received_at=NOW,
        provider="EASTMONEY",
        eligible_for_evaluation=True,
    )
    async with database.transaction() as session:
        session.add(security)
        await session.flush()
        session.add(subscription)
        await session.flush()
        session.add(subscription_revision)
        await session.flush()
        subscription.current_revision_id = subscription_revision.id
        session.add(target_revision)
        await session.flush()
        session.add(target_binding)
        session.add(quote_cycle)
        await session.flush()
        session.add(quote_item)
    return SimpleNamespace(
        security=security,
        subscription=subscription,
        target_revision=target_revision,
        target_binding=target_binding,
        quote_cycle=quote_cycle,
        quote_item=quote_item,
    )


def _command(seed, key: str, *, price_version: int = 10) -> SignalInput:
    return SignalInput(
        subscription_id=seed.subscription.id,
        security_id=seed.security.id,
        symbol=seed.security.symbol,
        security_name=seed.security.name,
        subscription_version=seed.subscription.version,
        price="8.50",
        price_at=NOW,
        quote_scheduled_at=NOW,
        price_version=price_version,
        target_revision_id=seed.target_revision.id,
        target_version=seed.target_binding.version,
        target_date=TARGET_DATE,
        targets=TARGETS,
        quote_cycle_id=seed.quote_cycle.id,
        quote_item_id=seed.quote_item.id,
        position_version=0,
        hysteresis_ratio="0.02",
        hysteresis_min="0.02",
        reason=EvaluationReason.SCHEDULED_QUOTE,
        idempotency_key=key,
        request_id=f"req-{key}"[:64],
    )


def _application(
    database: Database,
    notification_factory,
    repository_factory=SignalRepository,
):
    return SignalApplication(
        database,
        repository_factory=repository_factory,
        notification_factory=notification_factory,
    )


def _production_notification_factory(session):
    return TransactionalNotificationPublisher(
        session,
        target_resolver=_EmailTargetResolver(),
    )


async def _counts(database: Database, subscription_id):
    async with database.session() as session:
        state = await session.scalar(
            select(SignalState).where(SignalState.subscription_id == subscription_id)
        )
        evaluations = await session.scalar(
            select(func.count())
            .select_from(SignalEvaluation)
            .where(SignalEvaluation.subscription_id == subscription_id)
        )
        events = await session.scalar(
            select(func.count())
            .select_from(SignalEvent)
            .where(SignalEvent.subscription_id == subscription_id)
        )
        notifications = await session.scalar(
            select(func.count())
            .select_from(NotificationEvent)
            .where(NotificationEvent.business_object_id == str(subscription_id))
        )
        deliveries = await session.scalar(
            select(func.count())
            .select_from(NotificationDelivery)
            .join(NotificationEvent)
            .where(NotificationEvent.business_object_id == str(subscription_id))
        )
        outbox = await session.scalar(
            select(func.count())
            .select_from(EventOutbox)
            .where(EventOutbox.aggregate_id == str(subscription_id))
        )
    return state, evaluations, events, notifications, deliveries, outbox


@pytest.mark.anyio
async def test_concurrent_signal_transition_is_serialized_by_state_lock() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    seed = await _seed(database)
    barrier = _FirstIdempotencyReadBarrier(2)
    app = _application(
        database,
        _production_notification_factory,
        lambda session: _BarrierSignalRepository(session, barrier),
    )
    try:
        results = await asyncio.gather(
            app.evaluate(_command(seed, uuid4().hex, price_version=10)),
            app.evaluate(_command(seed, uuid4().hex, price_version=11)),
        )

        state, evaluations, events, notifications, deliveries, _ = await _counts(
            database, seed.subscription.id
        )
        assert state is not None
        assert (state.zone, state.version) == ("LOW", 2)
        assert evaluations == 2
        assert events == 1
        assert notifications == 1
        assert deliveries == 1
        assert sum(result.event is not None for result in results) == 1
        assert {result.result for result in results} <= {
            EvaluationResult.APPLIED,
            EvaluationResult.UNCHANGED,
            EvaluationResult.SUPERSEDED,
        }
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_concurrent_same_idempotency_key_replays_one_evaluation() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    seed = await _seed(database)
    barrier = _FirstIdempotencyReadBarrier(2)
    app = _application(
        database,
        _production_notification_factory,
        lambda session: _BarrierSignalRepository(session, barrier),
    )
    command = _command(seed, uuid4().hex)
    try:
        results = await asyncio.gather(app.evaluate(command), app.evaluate(command))

        state, evaluations, events, notifications, deliveries, _ = await _counts(
            database, seed.subscription.id
        )
        assert state is not None
        assert (state.zone, state.version) == ("LOW", 2)
        assert (evaluations, events, notifications, deliveries) == (1, 1, 1, 1)
        assert {result.evaluation.id for result in results} == {
            results[0].evaluation.id
        }
        assert sum(result.replayed for result in results) == 1
    finally:
        await database.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["notification_fact", "outbox"])
async def test_notification_database_failure_rolls_back_signal_transaction(
    failure: str,
) -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    seed = await _seed(database)
    app = _application(
        database,
        lambda session: _FailingDatabaseNotificationPublisher(session, failure),
    )
    try:
        with pytest.raises(AppError) as exc:
            await app.evaluate(_command(seed, uuid4().hex))
        assert exc.value.code == "SIGNAL_BACKEND_UNAVAILABLE"

        state, evaluations, events, notifications, deliveries, outbox = await _counts(
            database, seed.subscription.id
        )
        assert state is None
        assert (evaluations, events, notifications, deliveries, outbox) == (
            0,
            0,
            0,
            0,
            0,
        )
    finally:
        await database.dispose()
