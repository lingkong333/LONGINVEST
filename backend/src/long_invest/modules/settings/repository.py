from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.settings.models import (
    SecretValue,
    SystemSetting,
    SystemSettingHistory,
)


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_settings(self) -> list[SystemSetting]:
        rows = await self.session.scalars(
            select(SystemSetting).order_by(SystemSetting.key)
        )
        return list(rows.all())

    async def get_setting(
        self, key: str, *, lock: bool = False
    ) -> SystemSetting | None:
        statement = select(SystemSetting).where(SystemSetting.key == key)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def list_history(self, key: str) -> list[SystemSettingHistory]:
        rows = await self.session.scalars(
            select(SystemSettingHistory)
            .where(SystemSettingHistory.setting_key == key)
            .order_by(SystemSettingHistory.version.desc())
        )
        return list(rows.all())

    async def get_history(self, key: str, version: int) -> SystemSettingHistory | None:
        return await self.session.scalar(
            select(SystemSettingHistory).where(
                SystemSettingHistory.setting_key == key,
                SystemSettingHistory.version == version,
            )
        )

    async def get_secret(self, key: str, *, lock: bool = False) -> SecretValue | None:
        statement = select(SecretValue).where(SecretValue.key == key)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def list_secrets(self) -> list[SecretValue]:
        rows = await self.session.scalars(select(SecretValue).order_by(SecretValue.key))
        return list(rows.all())

    async def flush(self) -> None:
        await self.session.flush()
