from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from long_invest.modules.monitoring.contracts import (
    FrozenScheduleSubscriptions,
    FrozenSubscription,
    SubscriptionNotificationChannel,
    SubscriptionNotificationMode,
    SubscriptionNotificationPolicyView,
    SubscriptionStatus,
)
from long_invest.modules.monitoring.models import (
    MonitorSubscription,
    MonitorSubscriptionRevision,
)
from long_invest.platform.errors import AppError


@dataclass(frozen=True, slots=True)
class SubscriptionConfig:
    schedule_id: UUID | None = None
    schedule_revision_id: UUID | None = None
    target_mode: str = "MANUAL"
    target_version_id: UUID | None = None
    strategy_version_id: UUID | None = None
    parameters: dict[str, Any] | None = None
    hysteresis_ratio: Decimal = Decimal("0")
    hysteresis_min: Decimal = Decimal("0")
    notification_mode: str = SubscriptionNotificationMode.INHERIT
    notification_channels: tuple[str, ...] = ()
    reason: str = ""
    idempotency_key: str = ""
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionAuditContext:
    request_id: str
    actor_user_id: str
    session_id: str
    trusted_ip: str


@dataclass(frozen=True, slots=True)
class SubscriptionResult:
    subscription: MonitorSubscription
    revision: MonitorSubscriptionRevision
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    status: str


@dataclass(frozen=True, slots=True)
class SubscriptionEvent:
    subscription_id: UUID
    security_id: UUID
    symbol: str
    status: SubscriptionStatus
    version: int
    revision_id: UUID
    action: str
    reason: str
    idempotency_key: str
    request_digest: str
    request_id: str
    actor_user_id: str
    session_id: str
    trusted_ip: str
    before_summary: dict[str, Any] | None
    after_summary: dict[str, Any]


class MonitorSubscriptionService:
    def __init__(
        self,
        repository,
        *,
        audit,
        events,
        target_readiness,
        strategy_readiness,
        now=None,
    ):
        self.repo = repository
        self.audit = audit
        self.events = events
        self.targets = target_readiness
        self.strategies = strategy_readiness
        self.now = now or (lambda: datetime.now(UTC))

    async def list(self, *, include_archived=False):
        return await self.repo.list(include_archived=include_archived)

    async def get(self, subscription_id):
        row = await self.repo.get(subscription_id)
        if row is None:
            raise _error("MONITOR_SUBSCRIPTION_NOT_FOUND", "订阅不存在", 404)
        return row

    async def revisions(self, subscription_id):
        await self.get(subscription_id)
        return await self.repo.list_revisions(subscription_id)

    async def notification_policy(
        self, subscription_id: UUID, *, revision_id: UUID | None = None
    ) -> SubscriptionNotificationPolicyView:
        owner = await self.get(subscription_id)
        selected_revision_id = revision_id or owner.current_revision_id
        if selected_revision_id is None:
            raise _error("MONITOR_SUBSCRIPTION_CONFLICT", "订阅缺少当前修订", 409)
        revision = await self.repo.get_revision(subscription_id, selected_revision_id)
        if revision is None:
            raise _error(
                "MONITOR_SUBSCRIPTION_REVISION_NOT_FOUND", "订阅修订不存在", 404
            )
        return _notification_policy(owner, revision)

    async def configure_notification_policy(
        self,
        subscription_id: UUID,
        *,
        mode: SubscriptionNotificationMode | str,
        channels: tuple[SubscriptionNotificationChannel | str, ...],
        expected_version: int,
        reason: str,
        idempotency_key: str,
        audit_context: SubscriptionAuditContext | None = None,
    ) -> SubscriptionResult:
        owner = await self._lock(subscription_id)
        current = await self._revision(owner)
        normalized_mode, normalized_channels = _notification_selection(mode, channels)
        return await self.configure(
            subscription_id,
            SubscriptionConfig(
                schedule_id=current.schedule_id,
                schedule_revision_id=current.schedule_revision_id,
                target_mode=current.target_mode,
                target_version_id=current.target_version_id,
                strategy_version_id=current.strategy_version_id,
                parameters=dict(current.parameters),
                hysteresis_ratio=current.hysteresis_ratio,
                hysteresis_min=current.hysteresis_min,
                notification_mode=normalized_mode,
                notification_channels=normalized_channels,
                reason=reason,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
            ),
            audit_context=audit_context,
            action="notification_policy_changed",
            validate_strategy=False,
            request_payload={
                "mode": normalized_mode,
                "channels": list(normalized_channels),
                "expected_version": expected_version,
                "reason": reason.strip(),
            },
        )

    async def enabled_schedule_snapshots(self):
        grouped: dict[UUID, list[FrozenSubscription]] = {}
        for owner, revision in await self.repo.enabled_schedule_rows():
            grouped.setdefault(revision.schedule_id, []).append(
                FrozenSubscription(
                    subscription_id=owner.id,
                    security_id=owner.security_id,
                    symbol=owner.symbol,
                    version=owner.version,
                    revision_id=revision.id,
                )
            )
        return tuple(
            FrozenScheduleSubscriptions(
                schedule_id=schedule_id,
                subscriptions=tuple(
                    sorted(
                        subscriptions,
                        key=lambda item: (item.symbol, str(item.subscription_id)),
                    )
                ),
            )
            for schedule_id, subscriptions in sorted(
                grouped.items(), key=lambda item: str(item[0])
            )
        )

    async def create(
        self, *, security_id, symbol, config: SubscriptionConfig, audit_context=None
    ):
        context = _audit_context(audit_context)
        _validate_config(config)
        await self.repo.lock_security(security_id)
        digest = _digest(
            "created",
            security_id=str(security_id),
            symbol=symbol,
            config=_config_payload(config),
        )
        replay = await self.audit.find_replay(
            subscription_id=None, idempotency_key=config.idempotency_key
        )
        if replay is not None:
            _verify(replay, digest)
            owner = await self.get(replay.subscription_id)
            return await self._current(owner, True)
        existing = await self.repo.find_open_by_security(security_id)
        if existing is not None:
            return await self._current(existing, True)
        owner = MonitorSubscription(
            id=uuid4(),
            security_id=security_id,
            symbol=symbol,
            status=SubscriptionStatus.CONFIGURING,
            version=1,
        )
        await self.repo.create(owner)
        revision = _revision(owner.id, 1, config, digest, context)
        await self.repo.add_revision(revision)
        await self.repo.initialize_current(owner.id, revision.id)
        owner.current_revision_id = revision.id
        await self._emit(
            owner,
            revision,
            "created",
            config.reason,
            config.idempotency_key,
            digest,
            None,
            context,
        )
        return SubscriptionResult(owner, revision)

    async def configure(
        self,
        subscription_id,
        config: SubscriptionConfig,
        audit_context=None,
        *,
        action: str = "changed",
        validate_strategy: bool = True,
        request_payload: dict[str, Any] | None = None,
    ):
        context = _audit_context(audit_context)
        _validate_config(config)
        if config.expected_version is None:
            raise _version_conflict()
        owner = await self._lock(subscription_id)
        digest_payload = (
            {"config": _config_payload(config)}
            if request_payload is None
            else {"request": request_payload}
        )
        digest = _digest(action, subscription_id=str(subscription_id), **digest_payload)
        replay = await self.audit.find_replay(
            subscription_id=subscription_id, idempotency_key=config.idempotency_key
        )
        if replay is not None:
            _verify(replay, digest)
            return await self._current(owner, True)
        if (
            validate_strategy
            and config.target_mode == "STRATEGY"
            and (
                config.strategy_version_id is None
                or not await self.strategies.published_version(
                    config.strategy_version_id
                )
            )
        ):
            raise _error(
                "MONITOR_SUBSCRIPTION_NOT_READY",
                "订阅策略尚未发布或已失效",
                409,
            )
        if owner.version != config.expected_version:
            raise _version_conflict()
        self._active(owner)
        current = await self._revision(owner)
        before = _summary(owner, current)
        rows = await self.repo.list_revisions(subscription_id)
        revision = _revision(
            subscription_id,
            max((x.revision_no for x in rows), default=0) + 1,
            config,
            digest,
            context,
        )
        await self.repo.add_revision(revision)
        if not await self.repo.switch_revision(
            subscription_id,
            revision_id=revision.id,
            expected_version=config.expected_version,
        ):
            raise _version_conflict()
        owner.current_revision_id = revision.id
        owner.version = config.expected_version + 1
        await self._emit(
            owner,
            revision,
            action,
            config.reason,
            config.idempotency_key,
            digest,
            before,
            context,
        )
        return SubscriptionResult(owner, revision)

    async def enable(
        self,
        subscription_id,
        *,
        expected_version,
        reason,
        idempotency_key,
        audit_context=None,
    ):
        context = _audit_context(audit_context)
        owner = await self._lock(subscription_id)
        revision = await self._revision(owner)
        normalized_reason = _reason(reason)
        digest = _digest(
            "enabled",
            subscription_id=str(owner.id),
            expected_version=expected_version,
            reason=normalized_reason,
        )
        replay = await self.audit.find_replay(
            subscription_id=owner.id, idempotency_key=idempotency_key
        )
        if replay is not None:
            _verify(replay, digest)
            return await self._current(owner, True)
        if owner.version != expected_version:
            raise _version_conflict()
        self._active(owner)
        if not await self.targets.current_readiness(subscription_id):
            raise _error("MONITOR_SUBSCRIPTION_NOT_READY", "订阅目标尚未就绪", 409)
        if revision.target_mode == "STRATEGY" and (
            revision.strategy_version_id is None
            or not await self.strategies.published_version(revision.strategy_version_id)
        ):
            raise _error("MONITOR_SUBSCRIPTION_NOT_READY", "订阅策略尚未就绪", 409)
        return await self._transition(
            owner,
            revision,
            allowed={SubscriptionStatus.CONFIGURING, SubscriptionStatus.PAUSED},
            status=SubscriptionStatus.ENABLED,
            expected_version=expected_version,
            action="enabled",
            reason=normalized_reason,
            key=idempotency_key,
            context=context,
        )

    async def pause(
        self,
        subscription_id,
        *,
        expected_version,
        reason,
        idempotency_key,
        audit_context=None,
    ):
        context = _audit_context(audit_context)
        owner = await self._lock(subscription_id)
        revision = await self._revision(owner)
        return await self._transition(
            owner,
            revision,
            allowed={SubscriptionStatus.CONFIGURING, SubscriptionStatus.ENABLED},
            status=SubscriptionStatus.PAUSED,
            expected_version=expected_version,
            action="paused",
            reason=reason,
            key=idempotency_key,
            context=context,
        )

    async def archive(
        self,
        subscription_id,
        *,
        expected_version,
        reason,
        idempotency_key,
        audit_context=None,
    ):
        context = _audit_context(audit_context)
        owner = await self._lock(subscription_id)
        revision = await self._revision(owner)
        return await self._transition(
            owner,
            revision,
            allowed={SubscriptionStatus.PAUSED},
            status=SubscriptionStatus.ARCHIVED,
            expected_version=expected_version,
            action="archived",
            reason=reason,
            key=idempotency_key,
            context=context,
            archived_at=self.now(),
        )

    async def restore(
        self,
        subscription_id,
        *,
        expected_version,
        reason,
        idempotency_key,
        audit_context=None,
    ):
        context = _audit_context(audit_context)
        owner = await self._lock(subscription_id)
        revision = await self._revision(owner)
        return await self._transition(
            owner,
            revision,
            allowed={SubscriptionStatus.ARCHIVED},
            status=SubscriptionStatus.PAUSED,
            expected_version=expected_version,
            action="restored",
            reason=reason,
            key=idempotency_key,
            context=context,
            archived_at=None,
        )

    async def final_eligibility(self, snapshot: FrozenSubscription):
        owner = await self.repo.get(snapshot.subscription_id)
        if (
            owner is None
            or str(owner.status) != SubscriptionStatus.ENABLED
            or owner.version != snapshot.version
            or owner.current_revision_id != snapshot.revision_id
        ):
            return EligibilityResult("SUPERSEDED")
        return EligibilityResult("ELIGIBLE")

    async def execute_if_eligible(self, subscription_id, *, frozen_version, action):
        owner = await self._lock(subscription_id)
        if (
            str(owner.status) != SubscriptionStatus.ENABLED
            or owner.version != frozen_version
        ):
            return EligibilityResult("SUPERSEDED")
        revision = await self._revision(owner)
        snapshot = FrozenSubscription(
            subscription_id=owner.id,
            security_id=owner.security_id,
            symbol=owner.symbol,
            version=owner.version,
            revision_id=revision.id,
        )
        await action(self.repo.session, snapshot)
        return EligibilityResult("ELIGIBLE")

    async def _transition(
        self,
        owner,
        revision,
        *,
        allowed,
        status,
        expected_version,
        action,
        reason,
        key,
        context,
        archived_at=None,
    ):
        reason = _reason(reason)
        digest = _digest(
            action,
            subscription_id=str(owner.id),
            expected_version=expected_version,
            reason=reason,
        )
        replay = await self.audit.find_replay(
            subscription_id=owner.id, idempotency_key=key
        )
        if replay is not None:
            _verify(replay, digest)
            return await self._current(owner, True)
        if owner.version != expected_version:
            raise _version_conflict()
        if SubscriptionStatus(str(owner.status)) not in allowed:
            raise _error(
                "MONITOR_SUBSCRIPTION_CONFLICT", "订阅当前状态不允许该操作", 409
            )
        before = _summary(owner, revision)
        if not await self.repo.transition(
            owner.id,
            expected_status=owner.status,
            expected_version=expected_version,
            status=status,
            archived_at=archived_at,
        ):
            raise _version_conflict()
        owner.status = status
        owner.version = expected_version + 1
        owner.archived_at = archived_at
        await self._emit(owner, revision, action, reason, key, digest, before, context)
        return SubscriptionResult(owner, revision)

    async def _current(self, owner, replayed):
        return SubscriptionResult(owner, await self._revision(owner), replayed)

    async def _revision(self, owner):
        if owner.current_revision_id is None:
            raise _error("MONITOR_SUBSCRIPTION_CONFLICT", "订阅缺少当前修订", 409)
        row = await self.repo.get_revision(owner.id, owner.current_revision_id)
        if row is None:
            raise _error("MONITOR_SUBSCRIPTION_CONFLICT", "订阅修订不存在", 409)
        return row

    async def _lock(self, id):
        row = await self.repo.get(id, for_update=True)
        if row is None:
            raise _error("MONITOR_SUBSCRIPTION_NOT_FOUND", "订阅不存在", 404)
        return row

    def _active(self, owner):
        if owner.archived_at is not None:
            raise _error("MONITOR_SUBSCRIPTION_ARCHIVED", "订阅已归档", 409)

    async def _emit(
        self, owner, revision, action, reason, key, digest, before, context
    ):
        event = SubscriptionEvent(
            owner.id,
            owner.security_id,
            owner.symbol,
            SubscriptionStatus(str(owner.status)),
            owner.version,
            revision.id,
            action,
            reason,
            key,
            digest,
            context.get("request_id", "unknown"),
            context.get("actor_user_id", "unknown"),
            context.get("session_id", "unknown"),
            context.get("trusted_ip", "unknown"),
            before,
            _summary(owner, revision),
        )
        await self.audit.record(event)
        await self.events.publish(event)


def _revision(subscription_id, no, cfg, digest, context):
    notification_mode, notification_channels = _notification_selection(
        cfg.notification_mode, cfg.notification_channels
    )
    return MonitorSubscriptionRevision(
        id=uuid4(),
        subscription_id=subscription_id,
        revision_no=no,
        schedule_id=cfg.schedule_id,
        schedule_revision_id=cfg.schedule_revision_id,
        target_mode=cfg.target_mode,
        target_version_id=cfg.target_version_id,
        strategy_version_id=cfg.strategy_version_id,
        parameters=cfg.parameters or {},
        hysteresis_ratio=cfg.hysteresis_ratio,
        hysteresis_min=cfg.hysteresis_min,
        notification_mode=notification_mode,
        notification_channels=list(notification_channels),
        reason=_reason(cfg.reason),
        created_by_user_id=context.get("actor_user_id", "unknown"),
        request_id=context.get("request_id", "unknown"),
        idempotency_key=cfg.idempotency_key,
        content_hash=_digest("content", config=_config_payload(cfg)),
    )


def _config_payload(c):
    notification_mode, notification_channels = _notification_selection(
        c.notification_mode, c.notification_channels
    )
    return {
        "schedule_id": str(c.schedule_id) if c.schedule_id else None,
        "schedule_revision_id": str(c.schedule_revision_id)
        if c.schedule_revision_id
        else None,
        "target_mode": c.target_mode,
        "target_version_id": str(c.target_version_id) if c.target_version_id else None,
        "strategy_version_id": str(c.strategy_version_id)
        if c.strategy_version_id
        else None,
        "parameters": c.parameters or {},
        "hysteresis_ratio": str(c.hysteresis_ratio),
        "hysteresis_min": str(c.hysteresis_min),
        "notification_mode": notification_mode,
        "notification_channels": list(notification_channels),
        "reason": c.reason.strip(),
        "expected_version": c.expected_version,
    }


def _summary(o, r):
    return {
        "status": str(o.status),
        "version": o.version,
        "revision_id": str(r.id),
        "schedule_id": str(r.schedule_id) if r.schedule_id else None,
        "schedule_revision_id": str(r.schedule_revision_id)
        if r.schedule_revision_id
        else None,
        "archived": o.archived_at is not None,
        "notification_mode": r.notification_mode,
        "notification_channels": list(r.notification_channels),
    }


def _digest(action, **payload):
    return hashlib.sha256(
        json.dumps(
            {"action": action, **payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _reason(v):
    v = v.strip()
    if not v:
        raise _error(
            "MONITOR_SUBSCRIPTION_REASON_REQUIRED", "订阅操作原因不能为空", 422
        )
    return v


def _validate_config(c):
    _reason(c.reason)
    _notification_selection(c.notification_mode, c.notification_channels)
    if (
        not c.idempotency_key.strip()
        or c.target_mode not in {"MANUAL", "STRATEGY"}
        or c.hysteresis_ratio < 0
        or c.hysteresis_min < 0
        or (c.schedule_id is None) != (c.schedule_revision_id is None)
    ):
        raise _error("MONITOR_SUBSCRIPTION_CONFLICT", "订阅配置无效", 422)


def _notification_selection(mode, channels):
    try:
        normalized_mode = SubscriptionNotificationMode(str(mode))
        normalized = {
            SubscriptionNotificationChannel(str(channel)) for channel in channels
        }
    except ValueError as exc:
        raise _error(
            "MONITOR_SUBSCRIPTION_NOTIFICATION_POLICY_INVALID",
            "订阅通知策略无效",
            422,
        ) from exc
    if normalized_mode is SubscriptionNotificationMode.INHERIT and normalized:
        raise _error(
            "MONITOR_SUBSCRIPTION_NOTIFICATION_POLICY_INVALID",
            "继承通知策略时不能指定渠道",
            422,
        )
    ordered = tuple(
        channel.value
        for channel in SubscriptionNotificationChannel
        if channel in normalized
    )
    return normalized_mode.value, ordered


def _notification_policy(owner, revision):
    mode, channels = _notification_selection(
        revision.notification_mode, tuple(revision.notification_channels)
    )
    return SubscriptionNotificationPolicyView(
        subscription_id=owner.id,
        subscription_version=owner.version,
        revision_id=revision.id,
        revision_no=revision.revision_no,
        mode=mode,
        channels=channels,
    )


def _verify(r, d):
    if r.request_digest != d:
        raise _error("MONITOR_SUBSCRIPTION_CONFLICT", "幂等键已用于不同内容", 409)


def _version_conflict():
    return _error("MONITOR_SUBSCRIPTION_VERSION_CONFLICT", "订阅已被其他操作修改", 409)


def _error(code, message, status):
    return AppError(code=code, message=message, status_code=status)


def _audit_context(value):
    if value is None:
        return {
            "request_id": "unknown",
            "actor_user_id": "unknown",
            "session_id": "unknown",
            "trusted_ip": "unknown",
        }
    return {
        "request_id": value.request_id,
        "actor_user_id": value.actor_user_id,
        "session_id": value.session_id,
        "trusted_ip": value.trusted_ip,
    }
