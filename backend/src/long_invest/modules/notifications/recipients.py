from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select

from long_invest.modules.notifications.models import (
    NotificationRecipient,
    SignalNotificationBinding,
)
from long_invest.modules.settings.crypto import SecretCipher
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


class RecipientType(StrEnum):
    EMAIL = "EMAIL"
    WECOM_ROBOT = "WECOM_ROBOT"
    WECOM_USER = "WECOM_USER"


@dataclass(frozen=True, slots=True)
class RecipientInput:
    name: str
    recipient_type: RecipientType
    destination: str
    config: dict[str, Any]
    secret: str | None


class NotificationRecipientApplication:
    def __init__(self, database: Database, cipher: SecretCipher | None) -> None:
        self._database = database
        self._cipher = cipher

    async def list_recipients(self, *, enabled_only: bool = False):
        async with self._database.session() as session:
            statement = select(NotificationRecipient).order_by(
                NotificationRecipient.enabled.desc(), NotificationRecipient.name
            )
            if enabled_only:
                statement = statement.where(NotificationRecipient.enabled.is_(True))
            return list((await session.scalars(statement)).all())

    async def create(self, value: RecipientInput):
        recipient_id = uuid4()
        normalized = _validate(value, self._cipher, recipient_id, secret_required=True)
        async with self._database.transaction() as session:
            row = NotificationRecipient(
                id=recipient_id, version=1, enabled=True, **normalized
            )
            session.add(row)
            await session.flush()
            return row

    async def update(
        self, recipient_id: UUID, value: RecipientInput, expected_version: int
    ):
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(NotificationRecipient)
                .where(NotificationRecipient.id == recipient_id)
                .with_for_update()
            )
            if row is None:
                raise _error("NOTIFICATION_RECIPIENT_NOT_FOUND", "通知对象不存在", 404)
            if row.version != expected_version:
                raise _error(
                    "NOTIFICATION_RECIPIENT_VERSION_CONFLICT",
                    "通知对象已被其他操作修改",
                    409,
                )
            normalized = _validate(
                value,
                self._cipher,
                recipient_id,
                secret_required=row.secret_ciphertext is None,
            )
            for key in ("name", "recipient_type", "destination", "config"):
                setattr(row, key, normalized[key])
            if value.secret is not None:
                row.secret_ciphertext = normalized["secret_ciphertext"]
                row.secret_fingerprint = normalized["secret_fingerprint"]
            row.version += 1
            await session.flush()
            return row

    async def set_enabled(
        self, recipient_id: UUID, enabled: bool, expected_version: int
    ):
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(NotificationRecipient)
                .where(NotificationRecipient.id == recipient_id)
                .with_for_update()
            )
            if row is None:
                raise _error("NOTIFICATION_RECIPIENT_NOT_FOUND", "通知对象不存在", 404)
            if row.version != expected_version:
                raise _error(
                    "NOTIFICATION_RECIPIENT_VERSION_CONFLICT",
                    "通知对象已被其他操作修改",
                    409,
                )
            row.enabled = enabled
            row.version += 1
            await session.flush()
            return row

    async def get_binding(self, subscription_id: UUID):
        async with self._database.session() as session:
            return await session.get(SignalNotificationBinding, subscription_id)

    async def update_binding(
        self,
        subscription_id: UUID,
        recipient_ids: tuple[UUID, ...],
        expected_version: int,
        user_id: str,
    ):
        if not recipient_ids:
            raise _error(
                "MONITOR_NOTIFICATION_RECIPIENT_INVALID", "请至少选择一个通知对象", 422
            )
        unique_ids = tuple(dict.fromkeys(recipient_ids))
        async with self._database.transaction() as session:
            rows = list(
                (
                    await session.scalars(
                        select(NotificationRecipient).where(
                            NotificationRecipient.id.in_(unique_ids)
                        )
                    )
                ).all()
            )
            if len(rows) != len(unique_ids) or any(not row.enabled for row in rows):
                raise _error(
                    "MONITOR_NOTIFICATION_RECIPIENT_INVALID",
                    "通知对象不存在或已停用",
                    422,
                )
            binding = await session.get(
                SignalNotificationBinding, subscription_id, with_for_update=True
            )
            if binding is None:
                if expected_version != 0:
                    raise _error(
                        "SIGNAL_NOTIFICATION_BINDING_VERSION_CONFLICT",
                        "通知设置已变化",
                        409,
                    )
                binding = SignalNotificationBinding(
                    subscription_id=subscription_id,
                    recipient_ids=[str(item) for item in unique_ids],
                    version=1,
                    updated_by_user_id=user_id,
                )
                session.add(binding)
            else:
                if binding.version != expected_version:
                    raise _error(
                        "SIGNAL_NOTIFICATION_BINDING_VERSION_CONFLICT",
                        "通知设置已变化",
                        409,
                    )
                binding.recipient_ids = [str(item) for item in unique_ids]
                binding.version += 1
                binding.updated_by_user_id = user_id
            await session.flush()
            return binding

    async def test(self, recipient_id: UUID, *, message: str):
        from long_invest.modules.notifications.runtime import (
            NotificationDeliveryRuntime,
        )

        return await NotificationDeliveryRuntime(
            self._database, get_settings()
        ).test_recipient(recipient_id, message=message)


def get_notification_recipient_application() -> NotificationRecipientApplication:
    settings = get_settings()
    cipher = SecretCipher(settings.master_key) if settings.master_key else None
    return NotificationRecipientApplication(get_database(), cipher)


def _validate(
    value: RecipientInput,
    cipher: SecretCipher | None,
    recipient_id: UUID,
    *,
    secret_required: bool,
) -> dict[str, Any]:
    name = value.name.strip()
    destination = value.destination.strip()
    config = dict(value.config)
    if not name:
        raise _error("NOTIFICATION_RECIPIENT_INVALID", "名称不能为空", 422)
    if value.recipient_type is RecipientType.EMAIL:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", destination):
            raise _error("NOTIFICATION_RECIPIENT_INVALID", "邮箱地址格式不正确", 422)
        if value.secret:
            raise _error(
                "NOTIFICATION_RECIPIENT_INVALID", "邮箱对象不需要单独密钥", 422
            )
        config = {}
    elif value.recipient_type is RecipientType.WECOM_ROBOT:
        if secret_required and not value.secret:
            raise _error(
                "NOTIFICATION_RECIPIENT_INVALID", "企业微信机器人需要 Webhook", 422
            )
        if value.secret:
            parsed = urlsplit(value.secret)
            query = parse_qs(parsed.query)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "qyapi.weixin.qq.com"
                or parsed.path != "/cgi-bin/webhook/send"
                or set(query) != {"key"}
            ):
                raise _error(
                    "NOTIFICATION_RECIPIENT_INVALID",
                    "企业微信机器人 Webhook 格式不正确",
                    422,
                )
        destination = "企业微信群机器人"
        config = {}
    else:
        required = {"corp_id", "agent_id"}
        if not required.issubset(config) or not all(
            str(config[key]).strip() for key in required
        ):
            raise _error(
                "NOTIFICATION_RECIPIENT_INVALID",
                "企业微信企业用户需要企业 ID 和应用 AgentId",
                422,
            )
        if not destination or (secret_required and not value.secret):
            raise _error(
                "NOTIFICATION_RECIPIENT_INVALID",
                "企业微信企业用户需要成员账号和应用 Secret",
                422,
            )
        config = {
            "corp_id": str(config["corp_id"]).strip(),
            "agent_id": str(config["agent_id"]).strip(),
        }
    ciphertext = fingerprint = None
    if value.secret is not None:
        if cipher is None:
            raise _error("NOTIFICATION_SECRET_UNAVAILABLE", "服务器未配置通知密钥", 503)
        key = f"notification-recipient:{recipient_id}"
        ciphertext = cipher.encrypt(key, value.secret)
        fingerprint = cipher.fingerprint(key, value.secret)
    return {
        "name": name,
        "recipient_type": value.recipient_type.value,
        "destination": destination,
        "config": config,
        "secret_ciphertext": ciphertext,
        "secret_fingerprint": fingerprint,
    }


def _error(code: str, message: str, status: int) -> AppError:
    return AppError(code=code, message=message, status_code=status)
