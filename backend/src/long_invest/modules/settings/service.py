from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from long_invest.modules.settings.contracts import (
    SECRET_KEYS,
    SETTING_SCHEMAS,
    validate_setting,
)
from long_invest.modules.settings.crypto import SecretCipher
from long_invest.modules.settings.models import SecretValue, SystemSettingHistory
from long_invest.modules.settings.repository import SettingsRepository
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.errors import AppError
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class SettingsService:
    def __init__(
        self, repository: SettingsRepository, *, cipher: SecretCipher | None
    ) -> None:
        self._repository = repository
        self._cipher = cipher
        self._audit = AuditService(repository.session)
        self._outbox = TransactionalOutboxWriter()

    async def list_settings(self) -> list[dict[str, Any]]:
        return [
            self._setting_view(item) for item in await self._repository.list_settings()
        ]

    async def get_setting(self, key: str) -> dict[str, Any]:
        self._require_setting_key(key)
        item = await self._repository.get_setting(key)
        if item is None:
            raise _error("SETTING_NOT_FOUND", "配置不存在", 404)
        return self._setting_view(item)

    async def history(self, key: str) -> list[dict[str, Any]]:
        await self.get_setting(key)
        return [
            {
                "version": item.version,
                "value": item.value,
                "reason": item.reason,
                "actor_user_id": item.actor_user_id,
                "request_id": item.request_id,
                "created_at": item.created_at,
            }
            for item in await self._repository.list_history(key)
        ]

    async def update_setting(
        self,
        key: str,
        *,
        value: Any,
        expected_version: int,
        reason: str,
        request_id: str,
        idempotency_key: str,
        actor_user_id: str,
        session_id: str,
        trusted_ip: str,
    ) -> dict[str, Any]:
        self._require_setting_key(key)
        replay = await self._audit.find_by_idempotency(idempotency_key)
        if replay is not None:
            item = await self._repository.get_setting(key)
            if item is None or replay.object_id != key:
                raise _error("SETTING_IDEMPOTENCY_CONFLICT", "重复请求内容不一致", 409)
            return {**self._setting_view(item), "replayed": True}
        try:
            normalized = validate_setting(key, value)
        except ValidationError as exc:
            raise _error("SETTING_VALUE_INVALID", "配置内容不合法", 422) from exc
        item = await self._repository.get_setting(key, lock=True)
        if item is None:
            raise _error("SETTING_NOT_FOUND", "配置不存在", 404)
        if item.version != expected_version:
            raise _error("SETTING_VERSION_CONFLICT", "配置已被其他操作更新", 409)
        before = deepcopy(item.value)
        new_version = item.version + 1
        self._repository.session.add(
            SystemSettingHistory(
                setting_key=key,
                version=new_version,
                value=normalized,
                reason=reason,
                actor_user_id=actor_user_id,
                request_id=request_id,
            )
        )
        item.value = normalized
        item.version = new_version
        item.updated_by = actor_user_id
        await self._audit.append(
            AuditWrite(
                action_code="SETTING_UPDATED",
                object_type="SYSTEM_SETTING",
                object_id=key,
                result="SUCCESS",
                request_id=request_id,
                idempotency_key=idempotency_key,
                risk_level="HIGH",
                reason=reason,
                before_summary={"version": expected_version, "value": before},
                after_summary={"version": new_version, "value": normalized},
                actor_user_id=actor_user_id,
                session_id=session_id,
                trusted_ip=trusted_ip,
            )
        )
        await self._outbox.append(
            session=self._repository.session,
            topic="settings.changed.v1",
            aggregate_type="SYSTEM_SETTING",
            aggregate_id=key,
            queue="maintenance",
            payload={"key": key, "version": new_version},
            dedupe_key=f"setting:{key}:{new_version}",
        )
        await self._repository.flush()
        return {**self._setting_view(item), "replayed": False}

    async def rollback_setting(
        self, key: str, *, source_version: int, **context: Any
    ) -> dict[str, Any]:
        source = await self._repository.get_history(key, source_version)
        if source is None:
            raise _error("SETTING_HISTORY_NOT_FOUND", "历史版本不存在", 404)
        return await self.update_setting(key, value=source.value, **context)

    async def secret_statuses(self) -> list[dict[str, Any]]:
        stored = {item.key: item for item in await self._repository.list_secrets()}
        return [self._secret_view(key, stored.get(key)) for key in sorted(SECRET_KEYS)]

    async def update_secret(
        self,
        key: str,
        *,
        value: str | None,
        clear_secret: bool,
        expected_version: int,
        reason: str,
        request_id: str,
        idempotency_key: str,
        actor_user_id: str,
        session_id: str,
        trusted_ip: str,
    ) -> dict[str, Any]:
        if key not in SECRET_KEYS:
            raise _error("SECRET_KEY_NOT_ALLOWED", "不允许配置该密钥", 404)
        if clear_secret and value:
            raise _error("SECRET_COMMAND_INVALID", "清空和设置不能同时提交", 422)
        replay = await self._audit.find_by_idempotency(idempotency_key)
        if replay is not None:
            current = await self._repository.get_secret(key)
            if replay.object_id != key:
                raise _error("SECRET_IDEMPOTENCY_CONFLICT", "重复请求内容不一致", 409)
            return {**self._secret_view(key, current), "replayed": True}
        current = await self._repository.get_secret(key, lock=True)
        version = current.version if current else 0
        was_configured = bool(current and current.configured)
        if version != expected_version:
            raise _error("SECRET_VERSION_CONFLICT", "密钥已被其他操作更新", 409)
        if not clear_secret and (value is None or value == ""):
            return {**self._secret_view(key, current), "replayed": False}
        new_version = version + 1
        if clear_secret:
            if current is None:
                current = SecretValue(
                    key=key,
                    ciphertext=None,
                    configured=False,
                    version=new_version,
                    fingerprint="",
                    updated_by=actor_user_id,
                )
                self._repository.session.add(current)
            else:
                current.ciphertext = None
                current.configured = False
                current.version = new_version
                current.fingerprint = ""
                current.updated_by = actor_user_id
            fingerprint = None
            action = "SECRET_CLEARED"
        else:
            if self._cipher is None:
                raise _error("MASTER_KEY_NOT_CONFIGURED", "服务器尚未配置主密钥", 503)
            assert value is not None
            fingerprint = self._cipher.fingerprint(key, value)
            ciphertext = self._cipher.encrypt(key, value)
            if current is None:
                current = SecretValue(
                    key=key,
                    ciphertext=ciphertext,
                    configured=True,
                    version=new_version,
                    fingerprint=fingerprint,
                    updated_by=actor_user_id,
                )
                self._repository.session.add(current)
            else:
                current.ciphertext = ciphertext
                current.configured = True
                current.version = new_version
                current.fingerprint = fingerprint
                current.updated_by = actor_user_id
            action = "SECRET_UPDATED"
        await self._audit.append(
            AuditWrite(
                action_code=action,
                object_type="SECRET_VALUE",
                object_id=key,
                result="SUCCESS",
                request_id=request_id,
                idempotency_key=idempotency_key,
                risk_level="CRITICAL",
                reason=reason,
                before_summary={"configured": was_configured, "version": version},
                after_summary={
                    "configured": not clear_secret,
                    "version": new_version,
                    "fingerprint": fingerprint,
                },
                actor_user_id=actor_user_id,
                session_id=session_id,
                trusted_ip=trusted_ip,
            )
        )
        await self._outbox.append(
            session=self._repository.session,
            topic="secrets.changed.v1",
            aggregate_type="SECRET_VALUE",
            aggregate_id=key,
            queue="maintenance",
            payload={
                "key": key,
                "version": new_version,
                "configured": not clear_secret,
            },
            dedupe_key=f"secret:{key}:{new_version}:{action}",
        )
        await self._repository.flush()
        return {**self._secret_view(key, current), "replayed": False}

    async def resolve_secret(self, key: str) -> str | None:
        current = await self._repository.get_secret(key)
        if current is None or not current.configured:
            return None
        if self._cipher is None:
            raise _error("MASTER_KEY_NOT_CONFIGURED", "服务器尚未配置主密钥", 503)
        assert current.ciphertext is not None
        return self._cipher.decrypt(key, current.ciphertext)

    @staticmethod
    def _require_setting_key(key: str) -> None:
        if key not in SETTING_SCHEMAS:
            raise _error("SETTING_KEY_NOT_ALLOWED", "不允许配置该项目", 404)

    @staticmethod
    def _setting_view(item: Any) -> dict[str, Any]:
        return {
            "key": item.key,
            "value": item.value,
            "schema_version": item.schema_version,
            "version": item.version,
            "description": SETTING_SCHEMAS[item.key][1],
            "updated_by": item.updated_by,
            "updated_at": item.updated_at,
        }

    @staticmethod
    def _secret_view(key: str, item: Any | None) -> dict[str, Any]:
        return {
            "key": key,
            "configured": bool(item and item.configured),
            "masked": "********" if item and item.configured else None,
            "version": item.version if item is not None else 0,
            "fingerprint": item.fingerprint if item and item.configured else None,
            "updated_at": item.updated_at if item is not None else None,
        }


def _error(code: str, message: str, status: int) -> AppError:
    return AppError(code=code, message=message, status_code=status)


def transactional_settings_service(session, *, cipher: SecretCipher | None = None):
    return SettingsService(SettingsRepository(session), cipher=cipher)
