import asyncio
import base64
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from long_invest.modules.settings.crypto import SecretCipher
from long_invest.modules.settings.service import SettingsService
from long_invest.platform.errors import AppError


def sync_test(function):
    def wrapper():
        asyncio.run(function())

    return wrapper


class FakeSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, value) -> None:
        self.added.append(value)


class FakeRepository:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.setting = SimpleNamespace(
            key="notification.channel.wecom",
            value={"enabled": False, "timeout_seconds": 5.0},
            schema_version=1,
            version=1,
            updated_by=None,
            updated_at=datetime.now(UTC),
        )
        self.history = []
        self.secrets = {}

    async def list_settings(self):
        return [self.setting]

    async def get_setting(self, key, *, lock=False):
        del lock
        return self.setting if key == self.setting.key else None

    async def list_history(self, key):
        del key
        return self.history

    async def get_history(self, key, version):
        return next(
            (
                item
                for item in self.history
                if item.setting_key == key and item.version == version
            ),
            None,
        )

    async def get_secret(self, key, *, lock=False):
        del lock
        return self.secrets.get(key)

    async def list_secrets(self):
        return list(self.secrets.values())

    async def flush(self):
        for item in self.session.added:
            if hasattr(item, "setting_key") and item not in self.history:
                self.history.append(item)
            if hasattr(item, "ciphertext"):
                self.secrets[item.key] = item


class FakeAudit:
    def __init__(self) -> None:
        self.items = {}

    async def find_by_idempotency(self, key):
        return self.items.get(key)

    async def append(self, record):
        self.items[record.idempotency_key] = record
        return record


class FakeOutbox:
    def __init__(self) -> None:
        self.items = []

    async def append(self, **data):
        self.items.append(data)


def service(repository, *, with_key=True):
    cipher = None
    if with_key:
        cipher = SecretCipher(base64.urlsafe_b64encode(os.urandom(32)).decode())
    result = SettingsService(repository, cipher=cipher)
    result._audit = FakeAudit()
    result._outbox = FakeOutbox()
    return result


def context(key="request-1"):
    return {
        "reason": "测试变更",
        "request_id": "request-1",
        "idempotency_key": key,
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
    }


@sync_test
async def test_setting_update_is_versioned_audited_and_announced() -> None:
    repository = FakeRepository()
    subject = service(repository)

    result = await subject.update_setting(
        repository.setting.key,
        value={"enabled": True, "timeout_seconds": 3},
        expected_version=1,
        **context(),
    )

    assert result["version"] == 2
    assert result["replayed"] is False
    assert repository.history[0].value["enabled"] is True
    assert len(subject._outbox.items) == 1


@sync_test
async def test_setting_replay_does_not_create_another_version() -> None:
    repository = FakeRepository()
    subject = service(repository)
    command = {
        "value": {"enabled": True, "timeout_seconds": 3},
        "expected_version": 1,
        **context(),
    }

    await subject.update_setting(repository.setting.key, **command)
    result = await subject.update_setting(repository.setting.key, **command)

    assert result["replayed"] is True
    assert repository.setting.version == 2
    assert len(repository.history) == 1


@sync_test
async def test_setting_rejects_stale_version_and_unknown_key() -> None:
    repository = FakeRepository()
    subject = service(repository)
    with pytest.raises(AppError, match="配置已被其他操作更新"):
        await subject.update_setting(
            repository.setting.key,
            value={"enabled": True, "timeout_seconds": 3},
            expected_version=9,
            **context(),
        )
    with pytest.raises(AppError, match="不允许配置"):
        await subject.get_setting("database.url")


@sync_test
async def test_secret_is_never_returned_and_clear_preserves_version() -> None:
    repository = FakeRepository()
    subject = service(repository)
    updated = await subject.update_secret(
        "notification.email.password",
        value="private-password",
        clear_secret=False,
        expected_version=0,
        **context("secret-1"),
    )
    cleared = await subject.update_secret(
        "notification.email.password",
        value=None,
        clear_secret=True,
        expected_version=1,
        **context("secret-2"),
    )

    assert updated["masked"] == "********"
    assert "private-password" not in str(updated)
    assert cleared["configured"] is False
    assert cleared["version"] == 2
    assert await subject.resolve_secret("notification.email.password") is None


@sync_test
async def test_secret_requires_master_key_but_empty_value_means_keep() -> None:
    repository = FakeRepository()
    subject = service(repository, with_key=False)
    unchanged = await subject.update_secret(
        "notification.email.password",
        value="",
        clear_secret=False,
        expected_version=0,
        **context("empty"),
    )
    assert unchanged["configured"] is False
    with pytest.raises(AppError, match="主密钥"):
        await subject.update_secret(
            "notification.email.password",
            value="private-password",
            clear_secret=False,
            expected_version=0,
            **context("secret"),
        )
