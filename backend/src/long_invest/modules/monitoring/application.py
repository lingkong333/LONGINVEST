from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from long_invest.modules.monitor_schedules.application import (
    MonitorScheduleApplication,
    get_monitor_schedule_application,
)
from long_invest.modules.monitoring.outbox import MonitorSubscriptionOutbox
from long_invest.modules.monitoring.repository import MonitorSubscriptionRepository
from long_invest.modules.monitoring.service import (
    MonitorSubscriptionService,
    SubscriptionAuditContext,
    SubscriptionConfig,
)
from long_invest.modules.securities.application import (
    SecurityApplication,
    get_security_application,
)
from long_invest.modules.securities.contracts import ListingStatus, SecurityType
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


class UnavailableTargetReadiness:
    async def current_readiness(self, subscription_id):
        return False


class UnavailableStrategyReadiness:
    async def published_version(self, strategy_version_id):
        return False


class SubscriptionAudit:
    def __init__(self, session):
        self.session = session
        self.audit = AuditService(session)

    async def find_replay(self, *, subscription_id, idempotency_key):
        row = await self.audit.find_by_idempotency(
            _audit_key(subscription_id, idempotency_key)
        )
        if row is None:
            return None
        after = dict(row.after_summary or {})
        digest = after.pop("_request_digest", None)
        if not isinstance(digest, str):
            raise AppError(
                code="MONITOR_SUBSCRIPTION_CONFLICT",
                message="已有幂等记录不完整",
                status_code=409,
            )
        return SimpleNamespace(
            subscription_id=UUID(row.object_id),
            request_digest=digest,
            after_summary=after,
        )

    async def record(self, event):
        await self.audit.append(
            AuditWrite(
                action_code=f"monitor_subscription.{event.action}",
                object_type="monitor_subscription",
                object_id=str(event.subscription_id),
                result="SUCCESS",
                request_id=event.request_id,
                idempotency_key=_audit_key(
                    None if event.action == "created" else event.subscription_id,
                    event.idempotency_key,
                ),
                risk_level="HIGH",
                reason=event.reason,
                before_summary=event.before_summary,
                after_summary={
                    **event.after_summary,
                    "_request_digest": event.request_digest,
                },
                actor_user_id=event.actor_user_id,
                session_id=event.session_id,
                trusted_ip=event.trusted_ip,
            )
        )


class MonitorSubscriptionApplication:
    def __init__(
        self,
        database: Database,
        *,
        security_application: SecurityApplication,
        schedule_application: MonitorScheduleApplication,
        repository_factory=MonitorSubscriptionRepository,
        service_factory=MonitorSubscriptionService,
        audit_factory=SubscriptionAudit,
        event_factory=MonitorSubscriptionOutbox,
        target_readiness=None,
        strategy_readiness=None,
    ):
        self.db = database
        self.security = security_application
        self.schedules = schedule_application
        self.repo_factory = repository_factory
        self.service_factory = service_factory
        self.audit_factory = audit_factory
        self.event_factory = event_factory
        self.targets = target_readiness or UnavailableTargetReadiness()
        self.strategies = strategy_readiness or UnavailableStrategyReadiness()

    def _service(self, session):
        return self.service_factory(
            self.repo_factory(session),
            audit=self.audit_factory(session),
            events=self.event_factory(session),
            target_readiness=self.targets,
            strategy_readiness=self.strategies,
        )

    async def list(self, *, include_archived=False):
        return await self._read("list", include_archived=include_archived)

    async def get(self, subscription_id):
        return await self._read("get", subscription_id)

    async def revisions(self, subscription_id):
        return await self._read("revisions", subscription_id)

    async def enabled_schedule_snapshots(self):
        return await self._read("enabled_schedule_snapshots")

    async def create(
        self,
        *,
        symbol,
        schedule_id=None,
        reason,
        idempotency_key,
        audit_context: SubscriptionAuditContext | None = None,
        **config,
    ):
        identity = await self.security.resolve_identity(symbol)
        if (
            identity.security_type is not SecurityType.A_SHARE
            or identity.listing_status
            not in {
                ListingStatus.LISTED,
                ListingStatus.SUSPENDED,
            }
        ):
            raise AppError(
                code="MONITOR_SUBSCRIPTION_CONFLICT",
                message="只有当前上市或停牌的 A 股可以创建订阅",
                status_code=422,
            )
        schedule_revision_id = None
        if schedule_id is not None:
            schedule_revision_id = (
                await self.schedules.current_revision(schedule_id)
            ).id
        cfg = SubscriptionConfig(
            schedule_id=schedule_id,
            schedule_revision_id=schedule_revision_id,
            reason=reason,
            idempotency_key=idempotency_key,
            **config,
        )
        return await self._write(
            "create",
            security_id=identity.security_id,
            symbol=identity.symbol,
            config=cfg,
            audit_context=audit_context,
        )

    async def configure(
        self,
        subscription_id,
        *,
        reason,
        idempotency_key,
        expected_version,
        schedule_id=None,
        audit_context: SubscriptionAuditContext | None = None,
        **config,
    ):
        schedule_revision_id = None
        if schedule_id is not None:
            schedule_revision_id = (
                await self.schedules.current_revision(schedule_id)
            ).id
        cfg = SubscriptionConfig(
            schedule_id=schedule_id,
            schedule_revision_id=schedule_revision_id,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            **config,
        )
        return await self._write(
            "configure", subscription_id, cfg, audit_context=audit_context
        )

    async def enable(self, id, **kwargs):
        return await self._write("enable", id, **kwargs)

    async def pause(self, id, **kwargs):
        return await self._write("pause", id, **kwargs)

    async def archive(self, id, **kwargs):
        return await self._write("archive", id, **kwargs)

    async def restore(self, id, **kwargs):
        return await self._write("restore", id, **kwargs)

    async def final_eligibility(self, snapshot):
        return await self._read("final_eligibility", snapshot)

    async def execute_if_eligible(self, subscription_id, *, frozen_version, action):
        return await self._write(
            "execute_if_eligible",
            subscription_id,
            frozen_version=frozen_version,
            action=action,
        )

    async def _read(self, method, *args, **kwargs):
        try:
            async with self.db.session() as session:
                return await getattr(self._service(session), method)(*args, **kwargs)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _unavailable() from exc

    async def _write(self, method, *args, **kwargs):
        try:
            async with self.db.transaction() as session:
                return await getattr(self._service(session), method)(*args, **kwargs)
        except AppError:
            raise
        except IntegrityError as exc:
            raise AppError(
                code="MONITOR_SUBSCRIPTION_CONFLICT",
                message="该股票已有开放订阅或幂等请求冲突",
                status_code=409,
            ) from exc
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _unavailable() from exc


def get_monitor_subscription_application():
    return MonitorSubscriptionApplication(
        get_database(),
        security_application=get_security_application(),
        schedule_application=get_monitor_schedule_application(),
    )


def _audit_key(subscription_id, idempotency_key):
    digest = sha256(idempotency_key.encode()).hexdigest()
    return (
        f"monitor-subscription:{subscription_id}:{digest}"
        if subscription_id
        else f"monitor-subscription:create:{digest}"
    )


def _unavailable():
    return AppError(
        code="MONITOR_BACKEND_UNAVAILABLE",
        message="监控订阅服务暂时不可用",
        status_code=503,
    )
