from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any
from uuid import UUID

from long_invest.modules.notifications.admin import (
    DeliveryRetryBatch,
    DeliveryRetryFailure,
    NotificationAdminError,
    NotificationAdminRepository,
    NotificationAdminService,
)
from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.models import NotificationDelivery
from long_invest.modules.notifications.runtime import NotificationDeliveryRuntime
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


class NotificationAdminApplication:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        async with self._database.session() as session:
            try:
                return await getattr(
                    NotificationAdminService(NotificationAdminRepository(session)),
                    method,
                )(*args, **kwargs)
            except (NotificationAdminError, ValueError) as exc:
                raise _application_error(exc) from exc

    async def mutate(
        self,
        method: str,
        *args: Any,
        request_id: str,
        idempotency_key: str,
        actor_user_id: str,
        session_id: str,
        trusted_ip: str,
        reason: str,
    ) -> Any:
        async with self._database.transaction() as session:
            audit = AuditService(session)
            replay = await audit.find_by_idempotency(idempotency_key)
            if replay is not None:
                return await self._replay(session, method, args, replay)
            service = NotificationAdminService(NotificationAdminRepository(session))
            try:
                result = await getattr(service, method)(*args)
            except (NotificationAdminError, ValueError) as exc:
                raise _application_error(exc) from exc
            object_id, summary = _mutation_summary(method, result, args)
            await audit.append(
                AuditWrite(
                    action_code=f"NOTIFICATION_{method.upper()}",
                    object_type="NOTIFICATION_DELIVERY",
                    object_id=object_id,
                    result="SUCCESS",
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    risk_level="HIGH",
                    reason=reason,
                    before_summary=None,
                    after_summary=summary,
                    actor_user_id=actor_user_id,
                    session_id=session_id,
                    trusted_ip=trusted_ip,
                )
            )
            return result

    async def test_channel(
        self,
        channel: DeliveryChannel,
        *,
        message: str,
        request_id: str,
        idempotency_key: str,
        actor_user_id: str,
        session_id: str,
        trusted_ip: str,
        reason: str,
    ) -> dict[str, Any]:
        message_hash = hashlib.sha256(message.encode()).hexdigest()
        async with self._database.session() as session:
            replay = await AuditService(session).find_by_idempotency(idempotency_key)
            if replay is not None:
                if (
                    replay.object_id != channel.value
                    or (replay.after_summary or {}).get("message_hash") != message_hash
                ):
                    raise AppError(
                        code="NOTIFICATION_IDEMPOTENCY_CONFLICT",
                        message="重复请求内容不一致",
                        status_code=409,
                    )
                replayed = dict(replay.after_summary or {})
                replayed.pop("message_hash", None)
                return {**replayed, "replayed": True}
        result = await NotificationDeliveryRuntime(
            self._database, get_settings()
        ).test_channel(channel, message=message)
        safe_result = result.as_safe_dict()
        audit_result = {**safe_result, "message_hash": message_hash}
        async with self._database.transaction() as session:
            await AuditService(session).append(
                AuditWrite(
                    action_code="NOTIFICATION_CHANNEL_TESTED",
                    object_type="NOTIFICATION_CHANNEL",
                    object_id=channel.value,
                    result="SUCCESS" if result.outcome.value == "SUCCESS" else "FAILED",
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    risk_level="HIGH",
                    reason=reason,
                    before_summary=None,
                    after_summary=audit_result,
                    actor_user_id=actor_user_id,
                    session_id=session_id,
                    trusted_ip=trusted_ip,
                )
            )
        return {**safe_result, "replayed": False}

    @staticmethod
    async def _replay(session, method: str, args: tuple[Any, ...], audit) -> Any:
        summary = audit.after_summary or {}
        if summary.get("method") != method:
            raise AppError(
                code="NOTIFICATION_IDEMPOTENCY_CONFLICT",
                message="重复请求内容不一致",
                status_code=409,
            )
        if method in {"retry_delivery", "cancel_delivery"}:
            if summary.get("source_delivery_id") != str(args[0]):
                raise AppError(
                    code="NOTIFICATION_IDEMPOTENCY_CONFLICT",
                    message="重复请求内容不一致",
                    status_code=409,
                )
            delivery = await session.get(
                NotificationDelivery, UUID(summary["delivery_id"])
            )
            if delivery is None:
                raise AppError(
                    code="NOTIFICATION_RESOURCE_NOT_FOUND",
                    message="通知投递记录不存在",
                    status_code=404,
                )
            from long_invest.modules.notifications.admin import DeliveryMutation

            return DeliveryMutation(delivery=delivery, changed=False)
        if summary.get("source_delivery_ids") != [str(item) for item in args[0]]:
            raise AppError(
                code="NOTIFICATION_IDEMPOTENCY_CONFLICT",
                message="重复请求内容不一致",
                status_code=409,
            )
        deliveries = []
        for delivery_id in summary.get("retried_ids", []):
            delivery = await session.get(NotificationDelivery, UUID(delivery_id))
            if delivery is not None:
                deliveries.append(delivery)
        failures = tuple(
            DeliveryRetryFailure(UUID(item["delivery_id"]), item["code"])
            for item in summary.get("failures", [])
        )
        return DeliveryRetryBatch(tuple(deliveries), failures)


def _mutation_summary(method: str, result: Any, args: tuple[Any, ...]):
    if method in {"retry_delivery", "cancel_delivery"}:
        delivery_id = str(result.delivery.id)
        return delivery_id, {
            "method": method,
            "delivery_id": delivery_id,
            "source_delivery_id": str(args[0]),
        }
    retried_ids = [str(item.id) for item in result.retried]
    object_id = str(args[0][0]) if args and args[0] else "batch"
    return object_id, {
        "method": method,
        "retried_ids": retried_ids,
        "source_delivery_ids": [str(item) for item in args[0]],
        "failures": [
            {"delivery_id": str(item.delivery_id), "code": item.code}
            for item in result.failures
        ],
        "failure_count": len(result.failures),
    }


def _application_error(exc: Exception) -> AppError:
    code = getattr(exc, "code", "NOTIFICATION_REQUEST_INVALID")
    status = 404 if code == "NOTIFICATION_RESOURCE_NOT_FOUND" else 409
    if isinstance(exc, ValueError) and not isinstance(exc, NotificationAdminError):
        status = 422
    return AppError(code=code, message=str(exc), status_code=status)


@lru_cache
def get_notification_admin_application() -> NotificationAdminApplication:
    return NotificationAdminApplication(get_database())
