from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.auth.models import AppUser, UserSession


class AuthRepository(Protocol):
    async def find_user_by_username(self, username: str) -> AppUser | None: ...

    async def has_any_user(self) -> bool: ...

    async def get_user(self, user_id: UUID) -> AppUser | None: ...

    async def add_user(self, user: AppUser) -> AppUser: ...

    async def advance_password_version(
        self,
        user_id: UUID,
        *,
        expected_version: int,
        password_hash: str,
        changed_at: datetime,
    ) -> AppUser | None: ...

    async def find_session_by_digest(self, digest: str) -> UserSession | None: ...

    async def get_session(self, session_id: UUID) -> UserSession | None: ...

    async def list_sessions(self, user_id: UUID) -> list[UserSession]: ...

    async def add_session(self, session: UserSession) -> UserSession: ...

    async def flush(self) -> None: ...


class SqlAlchemyAuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_user_by_username(self, username: str) -> AppUser | None:
        return await self._session.scalar(
            select(AppUser).where(AppUser.username == username)
        )

    async def has_any_user(self) -> bool:
        count = await self._session.scalar(select(func.count()).select_from(AppUser))
        return bool(count)

    async def get_user(self, user_id: UUID) -> AppUser | None:
        return await self._session.scalar(select(AppUser).where(AppUser.id == user_id))

    async def add_user(self, user: AppUser) -> AppUser:
        self._session.add(user)
        return user

    async def advance_password_version(
        self,
        user_id: UUID,
        *,
        expected_version: int,
        password_hash: str,
        changed_at: datetime,
    ) -> AppUser | None:
        return await self._session.scalar(
            update(AppUser)
            .where(
                AppUser.id == user_id,
                AppUser.password_version == expected_version,
            )
            .values(
                password_hash=password_hash,
                password_version=AppUser.password_version + 1,
                password_changed_at=changed_at,
            )
            .returning(AppUser)
        )

    async def find_session_by_digest(self, digest: str) -> UserSession | None:
        return await self._session.scalar(
            select(UserSession).where(UserSession.token_digest == digest)
        )

    async def get_session(self, session_id: UUID) -> UserSession | None:
        return await self._session.scalar(
            select(UserSession).where(UserSession.id == session_id)
        )

    async def list_sessions(self, user_id: UUID) -> list[UserSession]:
        sessions = await self._session.scalars(
            select(UserSession)
            .where(UserSession.user_id == user_id)
            .order_by(UserSession.created_at.desc())
        )
        return list(sessions)

    async def add_session(self, session: UserSession) -> UserSession:
        self._session.add(session)
        return session

    async def flush(self) -> None:
        await self._session.flush()
