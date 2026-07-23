from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from long_invest.modules.positions.contracts import (
    PositionAction,
    PositionEvent,
    PositionEventSink,
    PositionResult,
    PositionStatus,
    PositionView,
    SetPosition,
)
from long_invest.modules.positions.models import UserPosition, UserPositionHistory
from long_invest.modules.positions.repository import PositionRepository
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.errors import AppError


class PositionService:
    def __init__(
        self,
        repository: PositionRepository,
        *,
        audit_service: AuditService | None = None,
        event_sink: PositionEventSink | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._audit = audit_service
        self._events = event_sink
        self._now = now or (lambda: datetime.now(UTC))

    async def get(self, security_id, *, symbol: str = ""):
        return _position_view(
            await self._repository.get_current(security_id),
            security_id=security_id,
            symbol=symbol,
        )

    async def list(self):
        return [_position_view(item) for item in await self._repository.list_current()]

    async def history(self, security_id=None):
        return await self._repository.list_history(security_id)

    async def list_page(self, *, page: int, page_size: int):
        rows, total = await self._repository.list_current_page(
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return tuple(_position_view(item) for item in rows), total

    async def history_page(
        self,
        security_id=None,
        *,
        page: int,
        page_size: int,
    ):
        return await self._repository.list_history_page(
            security_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )

    async def set(self, command: SetPosition) -> PositionResult:
        self._require_ports(command)
        await self._repository.lock_security(command.security_id)
        replay = await self._repository.find_history_by_idempotency(
            command.security_id, command.idempotency_key
        )
        if replay is not None:
            assert self._audit is not None and command.audit_context is not None
            replay_audit = await self._audit.find_by_idempotency(
                _audit_key(command.idempotency_key)
            )
            if (
                replay.after_status != command.target.value
                or replay.note != command.note
                or replay.source != command.source
                or replay.symbol != command.symbol
                or replay_audit is None
                or replay_audit.reason != command.audit_context.reason
            ):
                raise _idempotency_conflict()
            return PositionResult(
                code="POSITION_CHANGED",
                position=PositionView(
                    security_id=replay.security_id,
                    symbol=replay.symbol,
                    status=PositionStatus(replay.after_status),
                    version=replay.position_version,
                    source=replay.source,
                    updated_at=replay.effective_at,
                ),
                replayed=True,
            )

        assert self._audit is not None and command.audit_context is not None
        audit_replay = await self._audit.find_by_idempotency(
            _audit_key(command.idempotency_key)
        )
        if audit_replay is not None:
            expected = {
                "status": command.target.value,
                "note": command.note,
                "source": command.source,
                "symbol": command.symbol,
            }
            stored = audit_replay.after_summary or {}
            if (
                any(stored.get(key) != value for key, value in expected.items())
                or audit_replay.reason != command.audit_context.reason
            ):
                raise _idempotency_conflict()
            return PositionResult(
                code=(
                    "POSITION_UNCHANGED"
                    if audit_replay.result == "UNCHANGED"
                    else "POSITION_CHANGED"
                ),
                position=PositionView(
                    security_id=command.security_id,
                    symbol=command.symbol,
                    status=command.target,
                    version=int(stored.get("version", 0)),
                    source=command.source,
                ),
                replayed=True,
            )

        current = await self._repository.lock_current(command.security_id)
        current_version = current.version if current is not None else 0
        if (
            command.expected_version is not None
            and command.expected_version != current_version
        ):
            raise _version_conflict()
        before = (
            PositionStatus(current.status)
            if current is not None
            else PositionStatus.NOT_HOLDING
        )
        if before is command.target:
            view = _position_view(
                current, security_id=command.security_id, symbol=command.symbol
            )
            await self._append_audit(
                command, before, command.target, view.version, "UNCHANGED"
            )
            return PositionResult(code="POSITION_UNCHANGED", position=view)

        changed_at = self._now()
        version = current_version + 1
        history_id = uuid4()
        if current is None:
            current = UserPosition(
                id=uuid4(),
                security_id=command.security_id,
                symbol=command.symbol,
                status=command.target.value,
                version=version,
                source=command.source,
                updated_at=changed_at,
            )
        else:
            current.status = command.target.value
            current.version = version
            current.source = command.source
            current.updated_at = changed_at
        history = UserPositionHistory(
            id=history_id,
            position_id=current.id,
            security_id=command.security_id,
            symbol=command.symbol,
            before_status=before.value,
            after_status=command.target.value,
            effective_at=changed_at,
            note=command.note,
            source=command.source,
            request_id=command.request_id,
            idempotency_key=command.idempotency_key,
            actor_user_id=command.actor_user_id,
            position_version=version,
        )
        current.latest_history_id = history_id
        await self._repository.add_change(current, history)
        await self._append_audit(command, before, command.target, version, "SUCCESS")
        for event in _position_events(command, before, version, changed_at):
            assert self._events is not None
            await self._events.append(event)
        return PositionResult(code="POSITION_CHANGED", position=_position_view(current))

    def _require_ports(self, command: SetPosition) -> None:
        if (
            self._audit is None
            or self._events is None
            or command.audit_context is None
            or command.audit_context.idempotency_key != command.idempotency_key
        ):
            raise AppError(
                code="POSITION_TRANSACTION_PORT_UNAVAILABLE",
                message="持仓审计或可靠事件服务不可用",
                status_code=503,
            )

    async def _append_audit(self, command, before, after, version, result):
        assert self._audit is not None and command.audit_context is not None
        context = command.audit_context
        await self._audit.append(
            AuditWrite(
                action_code="POSITION_SET",
                object_type="user_position",
                object_id=str(command.security_id),
                result=result,
                request_id=context.request_id,
                idempotency_key=_audit_key(command.idempotency_key),
                risk_level="HIGH" if result == "SUCCESS" else "LOW",
                reason=context.reason,
                before_summary={
                    "status": before.value,
                    "version": max(0, version - 1),
                },
                after_summary={
                    "status": after.value,
                    "version": version,
                    "note": command.note,
                    "source": command.source,
                    "symbol": command.symbol,
                },
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )


def position_allowed_actions(
    status: PositionStatus | str,
) -> tuple[PositionAction, ...]:
    if PositionStatus(str(status)) is PositionStatus.HOLDING:
        return (PositionAction.CLEAR,)
    return (PositionAction.HOLD,)


def _position_view(position, *, security_id=None, symbol=None) -> PositionView:
    if position is None:
        return PositionView(
            security_id=security_id,
            symbol=symbol or "",
            status=PositionStatus.NOT_HOLDING,
            version=0,
        )
    return PositionView(
        security_id=position.security_id,
        symbol=position.symbol,
        status=PositionStatus(position.status),
        version=position.version,
        source=position.source,
        updated_at=position.updated_at,
    )


def _position_events(command, before, version, changed_at):
    payload = {
        "security_id": str(command.security_id),
        "symbol": command.symbol,
        "before_status": before.value,
        "after_status": command.target.value,
        "position_version": version,
        "request_id": command.request_id,
        "changed_at": changed_at.isoformat(),
    }
    suffixes = [("position.changed", "changed")]
    if command.target is PositionStatus.HOLDING:
        suffixes += [
            ("position.became_holding", "became-holding"),
            ("position.high_review_requested", "high-review"),
        ]
    else:
        suffixes += [
            ("position.became_not_holding", "became-not-holding"),
            (
                "high_notification_cancel_requested",
                "cancel-high-notifications",
            ),
        ]
    return tuple(
        PositionEvent(
            event_type=event_type,
            aggregate_id=str(command.security_id),
            dedupe_key=f"position:{command.security_id}:{version}:{suffix}",
            payload=payload,
        )
        for event_type, suffix in suffixes
    )


def _audit_key(idempotency_key: str) -> str:
    return "position:" + hashlib.sha256(idempotency_key.encode()).hexdigest()


def _version_conflict():
    return AppError(
        code="POSITION_VERSION_CONFLICT",
        message="持仓版本冲突，请刷新后重试",
        status_code=409,
    )


def _idempotency_conflict():
    return AppError(
        code="POSITION_IDEMPOTENCY_CONFLICT",
        message="同一幂等键已用于不同持仓状态",
        status_code=409,
    )
