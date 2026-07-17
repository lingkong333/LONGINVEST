from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.monitoring.application import MonitorSubscriptionApplication
from long_invest.modules.monitoring.contracts import SubscriptionStatus
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
