from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol
from uuid import UUID

from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityIdentity,
    SecurityType,
)
from long_invest.modules.watchlists.contracts import (
    WatchlistItemMutationResult,
    WatchlistItemRemovalResult,
    WatchlistItemView,
    WatchlistMutation,
    WatchlistView,
)
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.errors import AppError


class AuditPort(Protocol):
    async def append(self, data: AuditWrite) -> Any: ...


class EventPort(Protocol):
    async def updated(
        self,
        *,
        watchlist_id: UUID,
        action: str,
        symbol: str | None,
        version: int,
        reason: str,
    ) -> None: ...


class AuditContextPort(Protocol):
    request_id: str
    actor_user_id: str
    session_id: str
    trusted_ip: str


class WatchlistService:
    def __init__(self, repository: Any, audit: AuditPort, events: EventPort) -> None:
        self._repository = repository
        self._audit = audit
        self._events = events

    async def list(
        self, owner_user_id: UUID, *, include_archived: bool = False
    ) -> tuple[WatchlistView, ...]:
        records = await self._repository.list(
            owner_user_id, include_archived=include_archived
        )
        return tuple([await self._view(record) for record in records])

    async def get(self, watchlist_id: UUID, *, owner_user_id: UUID) -> WatchlistView:
        return await self._view(await self._owned(watchlist_id, owner_user_id))

    async def create(
        self,
        owner_user_id: UUID,
        command: WatchlistMutation,
        *,
        audit_context: AuditContextPort | None = None,
    ) -> WatchlistView:
        operation = "create"
        request_hash = _request_hash(
            operation=operation,
            owner_user_id=owner_user_id,
            name=command.name,
            description=command.description,
            display_order=command.display_order,
            reason=command.reason,
        )
        replay = await self._find_replay(
            owner_user_id=owner_user_id,
            operation=operation,
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
            context=audit_context,
        )
        if replay is not None:
            return WatchlistView.model_validate(replay)
        record = await self._repository.create(
            owner_user_id=owner_user_id,
            name=command.name,
            description=command.description,
            display_order=command.display_order,
            version=1,
        )
        result = await self._view(record)
        await self._write(
            record,
            operation=operation,
            action="created",
            symbol=None,
            reason=command.reason,
            key=command.idempotency_key,
            request_hash=request_hash,
            context=audit_context,
            response=result,
        )
        return result

    async def update(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        command: WatchlistMutation,
        audit_context: AuditContextPort | None = None,
    ) -> WatchlistView:
        if command.expected_version is None:
            raise _version_required()
        operation = "update"
        request_hash = _request_hash(
            operation=operation,
            watchlist_id=watchlist_id,
            owner_user_id=owner_user_id,
            name=command.name,
            description=command.description,
            display_order=command.display_order,
            reason=command.reason,
            expected_version=command.expected_version,
        )
        replay = await self._find_replay(
            owner_user_id=owner_user_id,
            operation=operation,
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
            context=audit_context,
        )
        if replay is not None:
            return WatchlistView.model_validate(replay)
        current = await self._owned(watchlist_id, owner_user_id, lock=True)
        self._require_active(current)
        record = await self._repository.update_version(
            watchlist_id,
            expected_version=command.expected_version,
            name=command.name,
            description=command.description,
            display_order=command.display_order,
        )
        result = await self._view(record)
        await self._write(
            record,
            operation=operation,
            action="updated",
            symbol=None,
            reason=command.reason,
            key=command.idempotency_key,
            request_hash=request_hash,
            context=audit_context,
            response=result,
        )
        return result

    async def archive(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        reason: str,
        idempotency_key: str,
        expected_version: int,
        audit_context: AuditContextPort | None = None,
    ) -> WatchlistView:
        operation = "archive"
        request_hash = _request_hash(
            operation=operation,
            watchlist_id=watchlist_id,
            owner_user_id=owner_user_id,
            reason=reason,
            expected_version=expected_version,
        )
        replay = await self._find_replay(
            owner_user_id=owner_user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            context=audit_context,
        )
        if replay is not None:
            return WatchlistView.model_validate(replay)
        current = await self._owned(watchlist_id, owner_user_id, lock=True)
        if current.archived_at is not None:
            raise AppError(
                code="WATCHLIST_ARCHIVED", message="监控分组已归档", status_code=409
            )
        record = await self._repository.archive(
            watchlist_id, expected_version=expected_version
        )
        result = await self._view(record)
        await self._write(
            record,
            operation=operation,
            action="archived",
            symbol=None,
            reason=reason,
            key=idempotency_key,
            request_hash=request_hash,
            context=audit_context,
            response=result,
        )
        return result

    async def add_item(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        security: SecurityIdentity,
        source: str,
        reason: str,
        idempotency_key: str,
        expected_version: int,
        audit_context: AuditContextPort | None = None,
    ) -> WatchlistItemMutationResult:
        operation = "add_item"
        request_hash = _request_hash(
            operation=operation,
            watchlist_id=watchlist_id,
            owner_user_id=owner_user_id,
            security_id=security.security_id,
            symbol=security.symbol,
            source=source,
            reason=reason,
            expected_version=expected_version,
        )
        replay = await self._find_replay(
            owner_user_id=owner_user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            context=audit_context,
        )
        if replay is not None:
            return WatchlistItemMutationResult.model_validate(replay)
        await self._lock_security(security.security_id)
        current = await self._owned(watchlist_id, owner_user_id, lock=True)
        self._require_active(current)
        self._require_eligible(security)
        existing = await self._repository.get_item(watchlist_id, security.security_id)
        if existing is not None:
            result = WatchlistItemMutationResult(
                item=_item_view(existing), version=current.version, created=False
            )
            await self._record(
                record=current,
                operation=operation,
                action="item-reused",
                symbol=security.symbol,
                reason=reason,
                key=idempotency_key,
                request_hash=request_hash,
                context=audit_context,
                response=result,
            )
            return result
        if current.version != expected_version:
            raise _version_conflict()
        item = await self._repository.add_item(
            watchlist_id,
            security_id=security.security_id,
            symbol=security.symbol,
            source=source,
        )
        record = await self._repository.update_version(
            watchlist_id, expected_version=expected_version
        )
        result = WatchlistItemMutationResult(
            item=_item_view(item), version=record.version, created=True
        )
        await self._write(
            record,
            operation=operation,
            action="item-added",
            symbol=security.symbol,
            reason=reason,
            key=idempotency_key,
            request_hash=request_hash,
            context=audit_context,
            response=result,
        )
        return result

    async def remove_item(
        self,
        watchlist_id: UUID,
        *,
        owner_user_id: UUID,
        security_id: UUID,
        symbol: str,
        reason: str,
        idempotency_key: str,
        expected_version: int,
        audit_context: AuditContextPort | None = None,
    ) -> WatchlistItemRemovalResult:
        operation = "remove_item"
        request_hash = _request_hash(
            operation=operation,
            watchlist_id=watchlist_id,
            owner_user_id=owner_user_id,
            security_id=security_id,
            symbol=symbol,
            reason=reason,
            expected_version=expected_version,
        )
        replay = await self._find_replay(
            owner_user_id=owner_user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            context=audit_context,
        )
        if replay is not None:
            return WatchlistItemRemovalResult.model_validate(replay)
        await self._lock_security(security_id)
        current = await self._owned(watchlist_id, owner_user_id, lock=True)
        self._require_active(current)
        if current.version != expected_version:
            raise _version_conflict()
        removed = await self._repository.remove_item(watchlist_id, security_id)
        if removed is None:
            result = WatchlistItemRemovalResult(
                removed=False, pause_recommended=False, version=current.version
            )
            await self._record(
                record=current,
                operation=operation,
                action="item-not-found",
                symbol=symbol,
                reason=reason,
                key=idempotency_key,
                request_hash=request_hash,
                context=audit_context,
                response=result,
            )
            return result
        record = await self._repository.update_version(
            watchlist_id, expected_version=expected_version
        )
        pause_recommended = await self._repository.count_memberships(security_id) == 0
        result = WatchlistItemRemovalResult(
            removed=True,
            pause_recommended=pause_recommended,
            version=record.version,
        )
        await self._write(
            record,
            operation=operation,
            action="item-removed",
            symbol=symbol,
            reason=reason,
            key=idempotency_key,
            request_hash=request_hash,
            context=audit_context,
            response=result,
        )
        return result

    async def _owned(
        self, watchlist_id: UUID, owner_user_id: UUID, *, lock: bool = False
    ) -> Any:
        record = await self._repository.get(watchlist_id, lock=lock)
        if record is None or record.owner_user_id != owner_user_id:
            raise AppError(
                code="WATCHLIST_NOT_FOUND", message="监控分组不存在", status_code=404
            )
        return record

    async def _view(self, record: Any) -> WatchlistView:
        items = (
            await self._repository.list_items(record.id)
            if hasattr(self._repository, "list_items")
            else getattr(record, "items", ())
        )
        return WatchlistView(
            id=record.id,
            owner_user_id=record.owner_user_id,
            name=record.name,
            description=record.description,
            display_order=record.display_order,
            version=record.version,
            archived=record.archived_at is not None,
            items=tuple(_item_view(item) for item in items),
        )

    async def _write(
        self,
        record: Any,
        *,
        operation: str,
        action: str,
        symbol: str | None,
        reason: str,
        key: str,
        request_hash: str,
        context: AuditContextPort | None,
        response: Any,
    ) -> None:
        await self._record(
            record=record,
            operation=operation,
            action=action,
            symbol=symbol,
            reason=reason,
            key=key,
            request_hash=request_hash,
            context=context,
            response=response,
        )
        await self._events.updated(
            watchlist_id=record.id,
            action=action,
            symbol=symbol,
            version=record.version,
            reason=reason,
        )

    async def _record(
        self,
        *,
        record: Any,
        operation: str,
        action: str,
        symbol: str | None,
        reason: str,
        key: str,
        request_hash: str,
        context: AuditContextPort | None,
        response: Any,
    ) -> None:
        context = context or _SystemAuditContext(
            request_id=f"watchlist-{hashlib.sha256(key.encode()).hexdigest()[:40]}",
            actor_user_id=str(record.owner_user_id),
        )
        await self._audit.append(
            AuditWrite(
                action_code=f"watchlist.{action}",
                object_type="watchlist",
                object_id=str(record.id),
                result="SUCCEEDED",
                request_id=context.request_id,
                idempotency_key=_idempotency_key(
                    owner_user_id=record.owner_user_id,
                    operation=operation,
                    raw_key=key,
                    context=context,
                ),
                risk_level="MEDIUM",
                reason=reason,
                before_summary=None,
                after_summary={
                    "request_hash": request_hash,
                    "version": record.version,
                    "symbol": symbol,
                    "response": response.model_dump(mode="json"),
                },
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )

    async def _find_replay(
        self,
        *,
        owner_user_id: UUID,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        context: AuditContextPort | None,
    ) -> dict[str, Any] | None:
        key = _idempotency_key(
            owner_user_id=owner_user_id,
            operation=operation,
            raw_key=idempotency_key,
            context=context,
        )
        replay = await self._repository.find_replay(key)
        if replay is None:
            return None
        self._require_same_replay(replay, request_hash)
        response = _replay_summary(replay).get("response")
        if not isinstance(response, dict):
            raise AppError(
                code="WATCHLIST_IDEMPOTENCY_CONFLICT",
                message="幂等记录缺少可重放结果",
                status_code=409,
            )
        return response

    async def _lock_security(self, security_id: UUID) -> None:
        lock = getattr(self._repository, "lock_security_memberships", None)
        if lock is not None:
            await lock(security_id)

    @staticmethod
    def _require_same_replay(replay: tuple, request_hash: str) -> None:
        if replay[0] != request_hash:
            raise AppError(
                code="WATCHLIST_IDEMPOTENCY_CONFLICT",
                message="相同幂等键不能用于不同请求",
                status_code=409,
            )

    @staticmethod
    def _require_active(record: Any) -> None:
        if record.archived_at is not None:
            raise AppError(
                code="WATCHLIST_ARCHIVED", message="监控分组已归档", status_code=409
            )

    @staticmethod
    def _require_eligible(security: SecurityIdentity) -> None:
        if (
            security.market not in {Market.SH, Market.SZ, Market.BJ}
            or security.security_type is not SecurityType.A_SHARE
            or security.listing_status
            not in {ListingStatus.LISTED, ListingStatus.SUSPENDED}
        ):
            raise AppError(
                code="WATCHLIST_ITEM_REJECTED",
                message="该股票不允许加入监控分组",
                status_code=422,
            )


def _item_view(item: Any) -> WatchlistItemView:
    return WatchlistItemView(
        id=item.id,
        watchlist_id=item.watchlist_id,
        security_id=item.security_id,
        symbol=item.symbol,
        source=item.source,
    )


def _request_hash(**values: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _normalize(values),
            default=str,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _idempotency_key(
    *,
    owner_user_id: UUID,
    operation: str,
    raw_key: str,
    context: AuditContextPort | None,
) -> str:
    actor = context.actor_user_id.strip() if context else str(owner_user_id)
    if len(actor) > 64:
        actor = hashlib.sha256(actor.encode()).hexdigest()
    digest = hashlib.sha256(raw_key.strip().encode()).hexdigest()
    value = f"watchlists:{actor}:{operation}:{digest}"
    if len(value) > 160:
        raise RuntimeError("watchlist idempotency namespace exceeds audit limit")
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return value.strip()
    if hasattr(value, "value"):
        return _normalize(value.value)
    if isinstance(value, UUID):
        return str(value)
    return value


def _replay_summary(replay: tuple) -> dict[str, Any]:
    return replay[2] if len(replay) > 2 and isinstance(replay[2], dict) else {}


def _version_conflict() -> AppError:
    return AppError(
        code="WATCHLIST_VERSION_CONFLICT",
        message="分组已被其他请求修改，请刷新后重试",
        status_code=409,
    )


def _version_required() -> AppError:
    return AppError(
        code="WATCHLIST_VERSION_REQUIRED",
        message="修改分组必须提供当前版本",
        status_code=422,
    )


class _SystemAuditContext:
    def __init__(self, *, request_id: str, actor_user_id: str) -> None:
        self.request_id = request_id
        self.actor_user_id = actor_user_id
        self.session_id = "system"
        self.trusted_ip = "unknown"
