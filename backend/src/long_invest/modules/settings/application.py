from __future__ import annotations

from functools import lru_cache
from typing import Any

from long_invest.modules.settings.crypto import SecretCipher
from long_invest.modules.settings.repository import SettingsRepository
from long_invest.modules.settings.service import SettingsService
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database, get_database


class SettingsApplication:
    def __init__(self, database: Database, cipher: SecretCipher | None) -> None:
        self._database = database
        self._cipher = cipher

    async def read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        async with self._database.session() as session:
            return await getattr(
                SettingsService(SettingsRepository(session), cipher=self._cipher),
                method,
            )(*args, **kwargs)

    async def write(self, method: str, *args: Any, **kwargs: Any) -> Any:
        async with self._database.transaction() as session:
            return await getattr(
                SettingsService(SettingsRepository(session), cipher=self._cipher),
                method,
            )(*args, **kwargs)


@lru_cache
def get_settings_application() -> SettingsApplication:
    key = get_settings().master_key
    return SettingsApplication(get_database(), SecretCipher(key) if key else None)
