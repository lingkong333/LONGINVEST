from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from long_invest.modules.targets.contracts import (
    ManualTargetCommand,
    RestoreTargetCommand,
    TargetBindingView,
    TargetMutationResult,
    TargetRevisionView,
    TargetSnapshot,
    TargetSource,
    TargetStatus,
    TargetValues,
)
from long_invest.modules.targets.models import TargetRevision
from long_invest.modules.targets.outbox import TargetEvent
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.errors import AppError

LARGE_CHANGE_THRESHOLD = Decimal("0.30")


class TargetSubscriptionPort(Protocol):
    async def lock(self, subscription_id: UUID) -> Any | None: ...

    async def switch_to_manual(self, **kwargs: Any) -> Any: ...


class TargetService:
    def __init__(
        self,
        repository,
        *,
        subscriptions: TargetSubscriptionPort,
        audit,
        events,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._subscriptions = subscriptions
        self._audit = audit
        self._events = events
        self._now = now or (lambda: datetime.now(UTC))

    async def set_manual(self, command: ManualTargetCommand) -> TargetMutationResult:
        subscription = await self._lock_subscription(command.subscription_id)
        request_digest = _request_digest(command)
        replay = await self._replay(command, request_digest)
        if replay is not None:
            return replay
        binding = await self._binding_for_new(subscription)
        self._check_version(binding.version, command.expected_version)
        await self._ensure_manual(subscription, command)

        current = await self._current_revision(binding)
        if (
            current is not None
            and _large_change(_values(current), command.values)
            and not command.large_change_confirmed
        ):
            raise _error(
                "TARGET_CONFIRMATION_REQUIRED",
                "目标变化超过 30%，需要二次确认",
                409,
            )
        revision = await self._new_revision(
            command=command,
            binding=binding,
            values=command.values,
            source=TargetSource.MANUAL,
            source_revision_id=None,
            target_date=command.target_date,
        )
        return await self._activate(
            command,
            binding,
            revision,
            action="target.manual_activated",
            request_digest=request_digest,
        )

    async def restore(self, command: RestoreTargetCommand) -> TargetMutationResult:
        subscription = await self._lock_subscription(command.subscription_id)
        request_digest = _request_digest(command)
        replay = await self._replay(command, request_digest)
        if replay is not None:
            return replay
        binding = await self._binding_for_new(subscription)
        source = await self._repository.get_revision(command.source_revision_id)
        if source is None or source.subscription_id != command.subscription_id:
            raise _error("TARGET_REVISION_NOT_FOUND", "目标历史版本不存在", 404)
        if binding.version != command.expected_version:
            raise _error("TARGET_RESTORE_STALE", "目标绑定已变化，请刷新后重试", 409)
        if not command.switch_to_manual_confirmed:
            raise _error(
                "TARGET_CONFIRMATION_REQUIRED", "恢复历史目标需要明确确认", 409
            )
        await self._ensure_manual(subscription, command)
        revision = await self._new_revision(
            command=command,
            binding=binding,
            values=_values(source),
            source=TargetSource.RESTORED,
            source_revision_id=source.id,
            target_date=source.target_date,
        )
        return await self._activate(
            command,
            binding,
            revision,
            action="target.restored",
            request_digest=request_digest,
        )

    async def list(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[tuple[TargetSnapshot, ...], int]:
        _validate_page(page, page_size)
        rows = await self._repository.list_current_rows(page=page, page_size=page_size)
        return (
            tuple(_snapshot(binding, revision) for binding, revision in rows),
            await self._repository.count_bindings(),
        )

    async def get(self, subscription_id: UUID) -> TargetSnapshot | None:
        binding = await self._repository.get_binding(subscription_id)
        return await self._snapshot(binding) if binding is not None else None

    async def history(
        self,
        subscription_id: UUID,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[tuple[TargetRevisionView, ...], int]:
        _validate_page(page, page_size)
        rows = await self._repository.list_revisions(
            subscription_id, page=page, page_size=page_size
        )
        return (
            tuple(_revision_view(row) for row in rows),
            await self._repository.count_revisions(subscription_id),
        )

    async def _lock_subscription(self, subscription_id):
        subscription = await self._subscriptions.lock(subscription_id)
        if subscription is None:
            raise _error("TARGET_SUBSCRIPTION_NOT_FOUND", "监控订阅不存在", 404)
        return subscription

    async def _binding_for_new(self, subscription):
        if str(subscription.status) == "ARCHIVED":
            raise _error("TARGET_SUBSCRIPTION_ARCHIVED", "监控订阅已归档", 409)
        binding = await self._repository.lock_binding(subscription.subscription_id)
        if binding is None:
            binding = await self._repository.create_binding(
                subscription.subscription_id
            )
        return binding

    async def _ensure_manual(self, subscription, command) -> None:
        if subscription.target_mode != "STRATEGY":
            return
        if not command.switch_to_manual_confirmed:
            raise _error(
                "TARGET_MODE_SWITCH_CONFIRMATION_REQUIRED",
                "策略模式需要确认切换为手工模式",
                409,
            )
        await self._subscriptions.switch_to_manual(
            subscription_id=command.subscription_id,
            expected_version=subscription.version,
            reason=command.reason,
            idempotency_key=f"{command.idempotency_key}:switch-manual",
            request_id=command.request_id,
            actor_user_id=command.actor_user_id,
            session_id=command.session_id,
            trusted_ip=command.trusted_ip,
        )

    async def _replay(self, command, request_digest):
        row = await self._repository.find_revision_by_idempotency(
            command.subscription_id, command.idempotency_key
        )
        if row is None:
            return None
        audit = await self._audit.find_by_idempotency(
            _audit_key(command.subscription_id, command.idempotency_key)
        )
        after = dict(audit.after_summary or {}) if audit is not None else {}
        if after.get("_request_digest") != request_digest:
            raise _error(
                "TARGET_IDEMPOTENCY_CONFLICT",
                "同一幂等键已用于不同目标内容",
                409,
            )
        try:
            binding = TargetBindingView(
                subscription_id=row.subscription_id,
                current_revision_id=row.id,
                status=TargetStatus.READY,
                version=int(after["binding_version"]),
                activated_at=datetime.fromisoformat(str(after["activated_at"])),
                stale_reason=None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(
                "TARGET_IDEMPOTENCY_CONFLICT",
                "幂等审计事实不完整",
                409,
            ) from exc
        return TargetMutationResult(
            code=(
                "TARGET_HISTORY_RESTORED"
                if row.source == TargetSource.RESTORED.value
                else "TARGET_MANUAL_ACTIVATED"
            ),
            binding=binding,
            revision=_revision_view(row),
            replayed=True,
        )

    async def _current_revision(self, binding):
        if binding.current_revision_id is None:
            return None
        return await self._repository.get_revision(binding.current_revision_id)

    async def _new_revision(
        self,
        *,
        command,
        binding,
        values,
        source,
        source_revision_id,
        target_date,
    ):
        revision = TargetRevision(
            id=uuid4(),
            subscription_id=command.subscription_id,
            revision_no=await self._repository.next_revision_no(
                command.subscription_id
            ),
            low_strong=values.low_strong,
            low_watch=values.low_watch,
            high_watch=values.high_watch,
            high_strong=values.high_strong,
            source=source.value,
            source_revision_id=source_revision_id,
            target_date=target_date,
            strategy_version_id=None,
            parameter_snapshot={},
            data_version=None,
            source_code_hash=None,
            content_hash=_content_hash(
                subscription_id=command.subscription_id,
                values=values,
                source=source,
                source_revision_id=source_revision_id,
                target_date=target_date,
                reason=command.reason,
            ),
            reason=command.reason,
            large_change_confirmed=getattr(command, "large_change_confirmed", False),
            request_id=command.request_id,
            idempotency_key=command.idempotency_key,
            actor_user_id=command.actor_user_id,
            session_id=command.session_id,
            trusted_ip=command.trusted_ip,
            created_at=self._now(),
        )
        await self._repository.persist_revision(revision)
        await self._repository.flush()
        return revision

    async def _activate(self, command, binding, revision, *, action, request_digest):
        before_revision_id = binding.current_revision_id
        binding.current_revision_id = revision.id
        binding.status = TargetStatus.READY.value
        binding.version += 1
        binding.activated_at = self._now()
        binding.stale_reason = None
        await self._audit.append(
            AuditWrite(
                action_code=action,
                object_type="target_revision",
                object_id=str(revision.id),
                result="SUCCESS",
                request_id=command.request_id,
                idempotency_key=_audit_key(
                    command.subscription_id, command.idempotency_key
                ),
                risk_level="HIGH",
                reason=command.reason,
                before_summary={
                    "revision_id": (
                        str(before_revision_id) if before_revision_id else None
                    )
                },
                after_summary={
                    "revision_id": str(revision.id),
                    "binding_version": binding.version,
                    "activated_at": binding.activated_at.isoformat(),
                    "_request_digest": request_digest,
                },
                actor_user_id=command.actor_user_id,
                session_id=command.session_id,
                trusted_ip=command.trusted_ip,
            )
        )
        payload = {
            "subscription_id": str(command.subscription_id),
            "revision_id": str(revision.id),
            "revision_no": revision.revision_no,
            "binding_version": binding.version,
            "reason": command.reason,
            "request_id": command.request_id,
        }
        suffix = (
            "restored"
            if revision.source == TargetSource.RESTORED.value
            else "activated"
        )
        await self._events.append(
            TargetEvent(
                event_type=(
                    "target.restored" if suffix == "restored" else "target.activated"
                ),
                aggregate_id=str(command.subscription_id),
                dedupe_key=f"target:{revision.id}:{suffix}",
                payload=payload,
            )
        )
        await self._events.append(
            TargetEvent(
                event_type="signal.reevaluation_requested",
                aggregate_id=str(command.subscription_id),
                dedupe_key=f"target:{revision.id}:signal-reevaluation",
                payload={**payload, "reason": "TARGET_ACTIVATED"},
            )
        )
        await self._repository.flush()
        return TargetMutationResult(
            code=(
                "TARGET_HISTORY_RESTORED"
                if suffix == "restored"
                else "TARGET_MANUAL_ACTIVATED"
            ),
            binding=_binding_view(binding),
            revision=_revision_view(revision),
        )

    async def _snapshot(self, binding) -> TargetSnapshot | None:
        if binding.current_revision_id is None or binding.activated_at is None:
            return None
        revision = await self._repository.get_revision(binding.current_revision_id)
        if revision is None:
            return None
        return _snapshot(binding, revision)

    @staticmethod
    def _check_version(actual, expected):
        if actual != expected:
            raise _error("TARGET_VERSION_CONFLICT", "目标绑定版本冲突", 409)


def relative_change(before: Decimal, after: Decimal) -> Decimal:
    return abs(after - before) / max(abs(before), Decimal("0.01"))


def _large_change(before: TargetValues, after: TargetValues) -> bool:
    return any(
        relative_change(old, new) > LARGE_CHANGE_THRESHOLD
        for old, new in zip(
            before.model_dump().values(),
            after.model_dump().values(),
            strict=True,
        )
    )


def _values(row) -> TargetValues:
    return TargetValues(
        low_strong=row.low_strong,
        low_watch=row.low_watch,
        high_watch=row.high_watch,
        high_strong=row.high_strong,
    )


def _binding_view(row) -> TargetBindingView:
    return TargetBindingView(
        subscription_id=row.subscription_id,
        current_revision_id=row.current_revision_id,
        status=TargetStatus(row.status),
        version=row.version,
        activated_at=row.activated_at,
        stale_reason=row.stale_reason,
    )


def _revision_view(row) -> TargetRevisionView:
    return TargetRevisionView(
        id=row.id,
        subscription_id=row.subscription_id,
        revision_no=row.revision_no,
        values=_values(row),
        source=TargetSource(row.source),
        source_revision_id=row.source_revision_id,
        target_date=row.target_date,
        strategy_version_id=row.strategy_version_id,
        parameter_snapshot=row.parameter_snapshot,
        data_version=row.data_version,
        source_code_hash=row.source_code_hash,
        content_hash=row.content_hash,
        reason=row.reason,
        created_at=row.created_at,
    )


def _snapshot(binding, revision) -> TargetSnapshot:
    return TargetSnapshot(
        subscription_id=binding.subscription_id,
        revision_id=revision.id,
        revision_no=revision.revision_no,
        binding_version=binding.version,
        values=_values(revision),
        source=TargetSource(revision.source),
        status=TargetStatus(binding.status),
        target_date=revision.target_date,
        strategy_version_id=revision.strategy_version_id,
        parameter_snapshot=revision.parameter_snapshot,
        data_version=revision.data_version,
        source_code_hash=revision.source_code_hash,
        content_hash=revision.content_hash,
        activated_at=binding.activated_at,
    )


def _request_digest(command) -> str:
    common = {
        "subscription_id": str(command.subscription_id),
        "reason": command.reason,
        "expected_version": command.expected_version,
        "actor_user_id": command.actor_user_id,
        "switch_to_manual_confirmed": command.switch_to_manual_confirmed,
    }
    if isinstance(command, ManualTargetCommand):
        payload = {
            **common,
            "action": "manual",
            "target_date": command.target_date.isoformat(),
            "values": command.values.model_dump(mode="json"),
            "large_change_confirmed": command.large_change_confirmed,
        }
    elif isinstance(command, RestoreTargetCommand):
        payload = {
            **common,
            "action": "restore",
            "source_revision_id": str(command.source_revision_id),
        }
    else:
        raise TypeError("unsupported target command")
    return _hash(payload)


def _content_hash(
    *, subscription_id, values, source, source_revision_id, target_date, reason
) -> str:
    return _hash(
        {
            "subscription_id": str(subscription_id),
            "values": {key: str(value) for key, value in values.model_dump().items()},
            "source": source.value,
            "source_revision_id": (
                str(source_revision_id) if source_revision_id else None
            ),
            "target_date": target_date.isoformat(),
            "reason": reason,
        }
    )


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _audit_key(subscription_id, key) -> str:
    return f"target:{subscription_id}:" + hashlib.sha256(key.encode()).hexdigest()


def _error(code, message, status_code):
    return AppError(code=code, message=message, status_code=status_code)


def _validate_page(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 200:
        raise ValueError("pagination is outside the supported range")
