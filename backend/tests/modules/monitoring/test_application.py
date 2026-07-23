from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.monitoring.application import (
    MonitorSubscriptionApplication,
    transactional_monitor_subscription_port,
)
from long_invest.modules.monitoring.contracts import (
    SubscriptionSignalSnapshot,
    SubscriptionStatus,
)
from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityIdentity,
    SecurityType,
)
from long_invest.platform.errors import AppError


class Database:
    def __init__(self, error=None):
        self.error = error

    @asynccontextmanager
    async def session(self):
        if self.error:
            raise self.error
        yield object()

    @asynccontextmanager
    async def transaction(self):
        if self.error:
            raise self.error
        yield object()


class SecurityApp:
    async def resolve_identity(self, symbol):
        return SecurityIdentity(
            security_id=uuid4(),
            symbol=symbol,
            market=Market.SH,
            security_type=SecurityType.A_SHARE,
            listing_status=ListingStatus.LISTED,
            is_suspended=False,
            is_st=False,
            listed_on=date(2000, 1, 1),
            delisted_on=None,
            master_version=1,
        )


class ScheduleApp:
    def __init__(self):
        self.revision_id = uuid4()

    async def current_revision(self, schedule_id):
        return SimpleNamespace(id=self.revision_id, schedule_id=schedule_id)


class Service:
    calls = []

    def __init__(self, repository, **kwargs):
        pass

    async def create(self, **kwargs):
        type(self).calls.append(kwargs)
        return SimpleNamespace(
            subscription=SimpleNamespace(status=SubscriptionStatus.CONFIGURING),
            revision=SimpleNamespace(
                schedule_revision_id=kwargs["config"].schedule_revision_id
            ),
            replayed=False,
        )


@pytest.mark.anyio
async def test_create_uses_public_identity_and_freezes_public_schedule_revision() -> (
    None
):
    from long_invest.modules.monitoring.service import SubscriptionAuditContext

    schedule = ScheduleApp()
    schedule_id = uuid4()
    app = MonitorSubscriptionApplication(
        Database(),
        security_application=SecurityApp(),
        schedule_application=schedule,
        repository_factory=lambda session: object(),
        service_factory=Service,
    )
    context = SubscriptionAuditContext(
        request_id="req-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )
    result = await app.create(
        symbol="600000.SH",
        schedule_id=schedule_id,
        reason="创建",
        idempotency_key="sub-1",
        audit_context=context,
    )
    assert result.subscription.status is SubscriptionStatus.CONFIGURING
    assert result.revision.schedule_revision_id == schedule.revision_id
    assert Service.calls[-1]["audit_context"] == context
    assert not hasattr(Service.calls[-1]["config"], "request_id")


@pytest.mark.anyio
async def test_database_failure_maps_to_503() -> None:
    app = MonitorSubscriptionApplication(
        Database(SQLAlchemyError("down")),
        security_application=SecurityApp(),
        schedule_application=ScheduleApp(),
        repository_factory=lambda session: object(),
        service_factory=Service,
    )
    with pytest.raises(AppError) as caught:
        await app.list()
    assert caught.value.code == "MONITOR_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503


@pytest.mark.anyio
async def test_create_rejects_non_a_share_before_transaction() -> None:
    class InvalidSecurity(SecurityApp):
        async def resolve_identity(self, symbol):
            identity = await super().resolve_identity(symbol)
            return SecurityIdentity(
                security_id=identity.security_id,
                symbol=identity.symbol,
                market=Market.SH,
                security_type=SecurityType.ETF,
                listing_status=ListingStatus.LISTED,
                is_suspended=False,
                is_st=False,
                listed_on=identity.listed_on,
                delisted_on=None,
                master_version=identity.master_version,
            )

    app = MonitorSubscriptionApplication(
        Database(),
        security_application=InvalidSecurity(),
        schedule_application=ScheduleApp(),
        repository_factory=lambda session: object(),
        service_factory=Service,
    )
    with pytest.raises(AppError) as caught:
        await app.create(symbol="600000.SH", reason="创建", idempotency_key="invalid")
    assert caught.value.code == "MONITOR_SUBSCRIPTION_CONFLICT"


@pytest.mark.anyio
async def test_check_now_submits_one_symbol_formal_quote_job() -> None:
    job = SimpleNamespace(id=uuid4(), status="PENDING_DISPATCH")
    quotes = SimpleNamespace(submit_manual=AsyncMock(return_value=job))
    owner = SimpleNamespace(id=uuid4(), symbol="600000.SH", status="ENABLED", version=3)
    app = MonitorSubscriptionApplication(
        Database(),
        security_application=SecurityApp(),
        schedule_application=ScheduleApp(),
        quote_application=quotes,
    )
    app.get = AsyncMock(return_value=owner)

    result = await app.check_now(
        owner.id,
        expected_version=3,
        idempotency_key="check-1",
        request_id="req-1",
        actor_user_id="user-1",
        reason="立即检查",
    )

    assert result is job
    quotes.submit_manual.assert_awaited_once_with(
        symbols=("600000.SH",),
        timeout_seconds=30,
        idempotency_key=(
            f"monitor-check:{owner.id}:"
            "1b8f9a4c240cd7cd72dad55f6dd41eb1d81e4ceb902fcca7bd3b4d9456de6c9b"
        ),
        request_id="req-1",
        created_by_user_id="user-1",
        reason="立即检查",
    )


@pytest.mark.anyio
async def test_check_now_rejects_paused_subscription_without_submitting() -> None:
    quotes = SimpleNamespace(submit_manual=AsyncMock())
    owner = SimpleNamespace(id=uuid4(), symbol="600000.SH", status="PAUSED", version=3)
    app = MonitorSubscriptionApplication(
        Database(),
        security_application=SecurityApp(),
        schedule_application=ScheduleApp(),
        quote_application=quotes,
    )
    app.get = AsyncMock(return_value=owner)

    with pytest.raises(AppError) as caught:
        await app.check_now(
            owner.id,
            expected_version=3,
            idempotency_key="check-1",
            request_id="req-1",
            actor_user_id="user-1",
            reason="立即检查",
        )

    assert caught.value.code == "MONITOR_SUBSCRIPTION_NOT_ENABLED"
    quotes.submit_manual.assert_not_awaited()


@pytest.mark.anyio
async def test_diagnostic_allows_paused_subscription_and_isolated_job() -> None:
    job = SimpleNamespace(id=uuid4(), status="PENDING_DISPATCH")
    quotes = SimpleNamespace(submit_diagnostic=AsyncMock(return_value=job))
    owner = SimpleNamespace(id=uuid4(), symbol="600000.SH", status="PAUSED", version=3)
    app = MonitorSubscriptionApplication(
        Database(),
        security_application=SecurityApp(),
        schedule_application=ScheduleApp(),
        quote_application=quotes,
    )
    app.get = AsyncMock(return_value=owner)

    result = await app.diagnose(
        owner.id,
        expected_version=3,
        idempotency_key="diagnose-1",
        request_id="req-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        reason="排查异常",
    )

    assert result is job
    quotes.submit_diagnostic.assert_awaited_once()
    assert quotes.submit_diagnostic.await_args.kwargs["idempotency_key"].startswith(
        f"monitor-diagnose:{owner.id}:"
    )


@pytest.mark.anyio
async def test_transactional_port_locks_and_returns_frozen_signal_snapshot() -> None:
    subscription_id = uuid4()
    security_id = uuid4()
    revision_id = uuid4()
    session = SimpleNamespace()

    class Repository:
        def __init__(self, received_session):
            assert received_session is session

        async def get(self, received_id, *, for_update=False):
            assert received_id == subscription_id
            assert for_update is True
            return SimpleNamespace(
                id=subscription_id,
                security_id=security_id,
                symbol="600000.SH",
                status="ENABLED",
                version=3,
                current_revision_id=revision_id,
            )

        async def get_revision(self, received_id, received_revision_id):
            assert (received_id, received_revision_id) == (
                subscription_id,
                revision_id,
            )
            return SimpleNamespace(
                id=revision_id,
                target_mode="STRATEGY",
                hysteresis_ratio=Decimal("0.010000"),
                hysteresis_min=Decimal("0.020000"),
                notification_mode="INHERIT",
                notification_channels=[],
            )

    port = transactional_monitor_subscription_port(
        session, repository_factory=Repository
    )
    snapshot = await port.lock(subscription_id)

    assert snapshot == SubscriptionSignalSnapshot(
        subscription_id=subscription_id,
        security_id=security_id,
        symbol="600000.SH",
        status=SubscriptionStatus.ENABLED,
        version=3,
        revision_id=revision_id,
        target_mode="STRATEGY",
        hysteresis_ratio=Decimal("0.010000"),
        hysteresis_min=Decimal("0.020000"),
        notification_mode="INHERIT",
        notification_channels=(),
    )
    with pytest.raises(ValidationError):
        snapshot.version = 4


@pytest.mark.anyio
async def test_transactional_port_switches_to_manual_without_committing() -> None:
    subscription_id = uuid4()
    revision_id = uuid4()
    calls = []

    class Session:
        async def commit(self):
            raise AssertionError("caller owns commit")

        async def close(self):
            raise AssertionError("caller owns close")

    session = Session()

    class Repository:
        def __init__(self, received_session):
            assert received_session is session

        async def get(self, received_id, *, for_update=False):
            assert received_id == subscription_id
            assert for_update is True
            return SimpleNamespace(
                id=subscription_id,
                current_revision_id=revision_id,
                version=3,
            )

        async def get_revision(self, received_id, received_revision_id):
            return SimpleNamespace(
                id=received_revision_id,
                schedule_id=None,
                schedule_revision_id=None,
                target_version_id=None,
                strategy_version_id=uuid4(),
                parameters={"lookback": 20},
                hysteresis_ratio=Decimal("0.01"),
                hysteresis_min=Decimal("0.02"),
                notification_mode="INHERIT",
                notification_channels=[],
            )

    class Service:
        def __init__(self, repository, **_ports):
            self.repository = repository

        async def configure(self, received_id, config, audit_context=None):
            calls.append((received_id, config, audit_context))
            return SimpleNamespace(revision=SimpleNamespace(id=uuid4()))

    port = transactional_monitor_subscription_port(
        session,
        repository_factory=Repository,
        service_factory=Service,
        audit_factory=lambda _session: object(),
        event_factory=lambda _session: object(),
    )
    await port.switch_to_manual(
        subscription_id=subscription_id,
        expected_version=3,
        reason="manual target",
        idempotency_key="idem:switch-manual",
        request_id="req-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )

    received_id, config, context = calls[0]
    assert received_id == subscription_id
    assert config.target_mode == "MANUAL"
    assert config.strategy_version_id is None
    assert config.parameters == {"lookback": 20}
    assert context.request_id == "req-1"
