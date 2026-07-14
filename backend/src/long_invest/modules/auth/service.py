from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from long_invest.modules.auth.contracts import (
    RequestActivity,
    SessionStatus,
    UserStatus,
)
from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.modules.auth.passwords import PasswordService
from long_invest.modules.auth.rate_limit import LoginRateLimitPolicy
from long_invest.modules.auth.repository import AuthRepository
from long_invest.modules.auth.session_policy import SessionPolicy
from long_invest.modules.auth.tokens import SessionCredentials, TokenService
from long_invest.modules.auth.validation import validate_new_password
from long_invest.platform.errors import AppError


@dataclass(frozen=True)
class LoginResult:
    session: UserSession
    credentials: SessionCredentials


@dataclass(frozen=True)
class AuthenticatedSession:
    user: AppUser
    session: UserSession


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        passwords: PasswordService,
        tokens: TokenService,
        rate_limiter: LoginRateLimitPolicy,
    ) -> None:
        self._repository = repository
        self._passwords = passwords
        self._tokens = tokens
        self._rate_limiter = rate_limiter
        self._sessions = SessionPolicy()
        self._dummy_hash = passwords.hash("fixed dummy password value")

    async def login(
        self,
        *,
        username: str,
        password: str,
        client_ip: str,
        user_agent_summary: str | None,
        now: datetime,
    ) -> LoginResult:
        decision = self._rate_limiter.check(
            ip=client_ip,
            username=username,
            now=now,
        )
        if not decision.allowed:
            raise AppError(
                code="AUTH_RATE_LIMITED",
                message="登录尝试过于频繁，请稍后再试",
                status_code=429,
                details={"retry_after_seconds": decision.retry_after_seconds},
            )

        user = await self._repository.find_user_by_username(username)
        encoded = user.password_hash if user is not None else self._dummy_hash
        verification = self._passwords.verify(password, encoded)
        if user is None or not verification.valid or user.status != UserStatus.ACTIVE:
            self._rate_limiter.record_failure(
                ip=client_ip,
                username=username,
                now=now,
            )
            raise _invalid_credentials()

        if verification.upgraded_hash is not None:
            user.password_hash = verification.upgraded_hash
        credentials = self._tokens.issue()
        session = self._sessions.new_session(
            user=user,
            token_digest=credentials.token_digest,
            csrf_digest=credentials.csrf_digest,
            now=now,
            client_ip=client_ip,
            user_agent_summary=user_agent_summary,
        )
        user.last_login_at = now
        user.last_login_ip = client_ip
        await self._repository.add_session(session)
        await self._repository.flush()
        self._rate_limiter.record_success(
            ip=client_ip,
            username=username,
            now=now,
        )
        return LoginResult(session=session, credentials=credentials)

    async def authenticate(
        self,
        *,
        token: str,
        now: datetime,
        activity: RequestActivity,
        client_ip: str | None = None,
    ) -> AuthenticatedSession:
        session = await self._repository.find_session_by_digest(
            self._tokens.digest(token)
        )
        if session is None:
            raise _invalid_session()
        user = await self._repository.get_user(session.user_id)
        if user is None:
            raise _invalid_session()
        status = self._sessions.record_request(
            session,
            user,
            now=now,
            activity=activity,
            client_ip=client_ip,
        )
        await self._repository.flush()
        if status is not SessionStatus.ACTIVE:
            raise _invalid_session(status)
        return AuthenticatedSession(user=user, session=session)

    async def revoke_session(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        now: datetime,
        reason: str,
    ) -> bool:
        session = await self._repository.get_session(session_id)
        if session is None or session.user_id != user_id:
            raise AppError(
                code="AUTH_SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )
        changed = self._sessions.revoke(session, now=now, reason=reason)
        await self._repository.flush()
        return changed

    async def revoke_other_sessions(
        self,
        *,
        user_id: UUID,
        current_session_id: UUID,
        now: datetime,
        reason: str,
    ) -> int:
        changed = 0
        for session in await self._repository.list_sessions(user_id):
            if session.id != current_session_id and self._sessions.revoke(
                session,
                now=now,
                reason=reason,
            ):
                changed += 1
        await self._repository.flush()
        return changed

    async def change_password(
        self,
        *,
        user_id: UUID,
        current_session_id: UUID,
        new_password: str,
        confirmation: str,
        client_ip: str | None,
        user_agent_summary: str | None,
        now: datetime,
    ) -> LoginResult:
        validate_new_password(new_password)
        if new_password != confirmation:
            raise AppError(
                code="AUTH_PASSWORD_MISMATCH",
                message="两次输入的密码不一致",
                status_code=422,
            )
        user = await self._repository.get_user(user_id)
        current = await self._repository.get_session(current_session_id)
        if user is None or current is None or current.user_id != user_id:
            raise _invalid_session()
        status = self._sessions.record_request(
            current,
            user,
            now=now,
            activity=RequestActivity.WRITE,
            client_ip=client_ip,
        )
        if status is not SessionStatus.ACTIVE:
            raise _invalid_session(status)

        user.password_hash = self._passwords.hash(new_password)
        user.password_version += 1
        user.password_changed_at = now
        for session in await self._repository.list_sessions(user_id):
            self._sessions.revoke(
                session,
                now=now,
                reason="password changed",
                status=SessionStatus.PASSWORD_CHANGED,
            )
        credentials = self._tokens.issue()
        rotated = self._sessions.new_session(
            user=user,
            token_digest=credentials.token_digest,
            csrf_digest=credentials.csrf_digest,
            now=now,
            client_ip=client_ip,
            user_agent_summary=user_agent_summary,
        )
        await self._repository.add_session(rotated)
        await self._repository.flush()
        return LoginResult(session=rotated, credentials=credentials)


def _invalid_credentials() -> AppError:
    return AppError(
        code="AUTH_INVALID_CREDENTIALS",
        message="用户名或密码错误",
        status_code=401,
    )


def _invalid_session(status: SessionStatus | None = None) -> AppError:
    details = {"status": status} if status is not None else None
    return AppError(
        code="AUTH_SESSION_INVALID",
        message="Session 已失效，请重新登录",
        status_code=401,
        details=details,
    )
