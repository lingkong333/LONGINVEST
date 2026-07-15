import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from long_invest.modules.auth.audit import (
    AuditContext,
    AuthAuditPort,
    build_auth_audit_event,
)
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
from long_invest.modules.auth.tokens import (
    CsrfCredentials,
    SessionCredentials,
    TokenService,
)
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


@dataclass
class _LoginReservation:
    reservation_id: str | None
    failure_counted: bool = False


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        passwords: PasswordService,
        tokens: TokenService,
        rate_limiter: LoginRateLimitPolicy,
        audit: AuthAuditPort,
        audit_context: AuditContext,
        *,
        dummy_password_hash: str,
    ) -> None:
        self._repository = repository
        self._passwords = passwords
        self._tokens = tokens
        self._rate_limiter = rate_limiter
        self._audit = audit
        self._audit_context = audit_context
        self._sessions = SessionPolicy()
        self._dummy_hash = dummy_password_hash

    async def login(
        self,
        *,
        username: str,
        password: str,
        client_ip: str,
        user_agent_summary: str | None,
        now: datetime,
    ) -> LoginResult:
        decision = await self._rate_limiter.check(
            ip=client_ip,
            username=username,
            now=now,
        )
        if not decision.allowed:
            await self._record_audit(
                action_code="AUTH_LOGIN",
                object_type="app_user",
                object_id=username,
                result="DENIED",
                risk_level="HIGH",
                reason="rate_limited",
            )
            raise AppError(
                code="AUTH_RATE_LIMITED",
                message="登录尝试过于频繁，请稍后再试",
                status_code=429,
                details={"retry_after_seconds": decision.retry_after_seconds},
            )

        reservation = _LoginReservation(decision.reservation_id)
        try:
            return await self._login_with_reservation(
                username=username,
                password=password,
                client_ip=client_ip,
                user_agent_summary=user_agent_summary,
                now=now,
                reservation=reservation,
            )
        finally:
            if not reservation.failure_counted:
                release = asyncio.create_task(
                    self._rate_limiter.record_success(
                        ip=client_ip,
                        username=username,
                        now=now,
                        reservation_id=reservation.reservation_id,
                    )
                )
                try:
                    await asyncio.shield(release)
                except asyncio.CancelledError:
                    await release
                    raise

    async def _login_with_reservation(
        self,
        *,
        username: str,
        password: str,
        client_ip: str,
        user_agent_summary: str | None,
        now: datetime,
        reservation: _LoginReservation,
    ) -> LoginResult:

        user = await self._repository.find_user_by_username(username)
        encoded = user.password_hash if user is not None else self._dummy_hash
        verification = self._passwords.verify(password, encoded)
        if user is None or not verification.valid or user.status != UserStatus.ACTIVE:
            await self._rate_limiter.record_failure(
                ip=client_ip,
                username=username,
                now=now,
                reservation_id=reservation.reservation_id,
            )
            reservation.failure_counted = True
            await self._record_audit(
                action_code="AUTH_LOGIN",
                object_type="app_user",
                object_id=username,
                result="FAILED",
                risk_level="HIGH",
                reason="invalid_credentials",
                actor_user_id=str(user.id) if user is not None else None,
            )
            raise _invalid_credentials()

        if verification.upgraded_hash is not None:
            replaced = await self._repository.replace_password_hash(
                user.id,
                expected_version=user.password_version,
                expected_hash=encoded,
                replacement_hash=verification.upgraded_hash,
            )
            if not replaced:
                await self._rate_limiter.record_failure(
                    ip=client_ip,
                    username=username,
                    now=now,
                    reservation_id=reservation.reservation_id,
                )
                reservation.failure_counted = True
                await self._record_audit(
                    action_code="AUTH_LOGIN",
                    object_type="app_user",
                    object_id=str(user.id),
                    result="DENIED",
                    risk_level="HIGH",
                    reason="password_hash_upgrade_conflict",
                    actor_user_id=str(user.id),
                )
                raise _invalid_credentials()
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
        await self._record_audit(
            action_code="AUTH_LOGIN",
            object_type="app_user",
            object_id=str(user.id),
            result="SUCCESS",
            risk_level="MEDIUM",
            after_summary={"password_version": user.password_version},
            actor_user_id=str(user.id),
            session_id=str(session.id),
        )
        await self._repository.flush()
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

    async def issue_csrf(
        self,
        *,
        session_token: str,
        now: datetime,
        client_ip: str | None = None,
    ) -> CsrfCredentials:
        authenticated = await self.authenticate(
            token=session_token,
            now=now,
            activity=RequestActivity.BACKGROUND,
            client_ip=client_ip,
        )
        credentials = self._tokens.issue_csrf()
        authenticated.session.csrf_secret_digest = credentials.csrf_digest
        await self._repository.flush()
        return credentials

    async def validate_csrf(
        self,
        *,
        session_token: str,
        csrf_token: str,
        now: datetime,
        client_ip: str | None = None,
    ) -> AuthenticatedSession:
        authenticated = await self.authenticate(
            token=session_token,
            now=now,
            activity=RequestActivity.BACKGROUND,
            client_ip=client_ip,
        )
        if not self._tokens.verify_digest(
            csrf_token,
            authenticated.session.csrf_secret_digest,
        ):
            raise AppError(
                code="AUTH_CSRF_INVALID",
                message="CSRF 校验失败",
                status_code=403,
            )
        self._sessions.record_request(
            authenticated.session,
            authenticated.user,
            now=now,
            activity=RequestActivity.WRITE,
            client_ip=client_ip,
        )
        await self._repository.flush()
        return authenticated

    async def validate_replay_credentials(
        self,
        *,
        session_token: str,
        csrf_token: str,
        expected_session_id: str | None,
    ) -> UserSession:
        session = await self._repository.find_session_by_digest(
            self._tokens.digest(session_token)
        )
        if (
            session is None
            or str(session.id) != expected_session_id
            or session.status not in {SessionStatus.ACTIVE, SessionStatus.REVOKED}
        ):
            raise _invalid_session()
        if not self._tokens.verify_digest(csrf_token, session.csrf_secret_digest):
            raise AppError(
                code="AUTH_CSRF_INVALID",
                message="CSRF 校验失败",
                status_code=403,
            )
        return session

    async def revoke_session(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        now: datetime,
        reason: str,
        actor_session_id: UUID | None = None,
        action_code: str = "AUTH_SESSION_REVOKE",
    ) -> bool:
        session = await self._repository.get_session(session_id)
        if session is None or session.user_id != user_id:
            raise AppError(
                code="AUTH_SESSION_NOT_FOUND",
                message="Session 不存在",
                status_code=404,
            )
        changed = self._sessions.revoke(session, now=now, reason=reason)
        await self._record_audit(
            action_code=action_code,
            object_type="user_session",
            object_id=str(session.id),
            result="SUCCESS" if changed else "NOOP",
            risk_level="HIGH",
            reason=reason,
            before_summary={
                "status": SessionStatus.ACTIVE if changed else session.status
            },
            after_summary={"status": session.status},
            actor_user_id=str(user_id),
            session_id=str(actor_session_id) if actor_session_id else None,
        )
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
        await self._record_audit(
            action_code="AUTH_SESSION_REVOKE_OTHERS",
            object_type="app_user",
            object_id=str(user_id),
            result="SUCCESS" if changed else "NOOP",
            risk_level="HIGH",
            reason=reason,
            after_summary={"revoked_count": changed},
            actor_user_id=str(user_id),
            session_id=str(current_session_id),
        )
        await self._repository.flush()
        return changed

    async def revoke_all_sessions(
        self,
        *,
        user_id: UUID,
        current_session_id: UUID,
        now: datetime,
        reason: str,
    ) -> int:
        changed = 0
        for session in await self._repository.list_sessions(user_id):
            if self._sessions.revoke(session, now=now, reason=reason):
                changed += 1
        await self._record_audit(
            action_code="AUTH_SESSION_REVOKE_ALL",
            object_type="app_user",
            object_id=str(user_id),
            result="SUCCESS" if changed else "NOOP",
            risk_level="HIGH",
            reason=reason,
            after_summary={"revoked_count": changed},
            actor_user_id=str(user_id),
            session_id=str(current_session_id),
        )
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

        user = await self._repository.advance_password_version(
            user_id,
            expected_version=current.password_version,
            password_hash=self._passwords.hash(new_password),
            changed_at=now,
        )
        if user is None:
            await self._record_audit(
                action_code="AUTH_PASSWORD_CHANGE",
                object_type="app_user",
                object_id=str(user_id),
                result="DENIED",
                risk_level="CRITICAL",
                reason="password_version_conflict",
                before_summary={"password_version": current.password_version},
                actor_user_id=str(user_id),
                session_id=str(current_session_id),
            )
            raise _invalid_session(SessionStatus.PASSWORD_CHANGED)
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
        await self._record_audit(
            action_code="AUTH_PASSWORD_CHANGE",
            object_type="app_user",
            object_id=str(user_id),
            result="SUCCESS",
            risk_level="CRITICAL",
            before_summary={"password_version": current.password_version},
            after_summary={"password_version": user.password_version},
            actor_user_id=str(user_id),
            session_id=str(current_session_id),
        )
        await self._repository.flush()
        return LoginResult(session=rotated, credentials=credentials)

    async def _record_audit(
        self,
        *,
        action_code: str,
        object_type: str,
        object_id: str,
        result: str,
        risk_level: str,
        reason: str | None = None,
        before_summary: dict[str, object] | None = None,
        after_summary: dict[str, object] | None = None,
        actor_user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        await self._audit.record(
            build_auth_audit_event(
                self._audit_context,
                action_code=action_code,
                object_type=object_type,
                object_id=object_id,
                result=result,
                risk_level=risk_level,
                reason=reason,
                before_summary=before_summary,
                after_summary=after_summary,
                actor_user_id=actor_user_id,
                session_id=session_id,
            )
        )


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
