from datetime import datetime
from uuid import uuid4

from long_invest.modules.auth.audit import (
    AuditContext,
    AuthAuditPort,
    build_auth_audit_event,
)
from long_invest.modules.auth.contracts import SessionStatus, UserStatus
from long_invest.modules.auth.models import AppUser
from long_invest.modules.auth.passwords import PasswordService
from long_invest.modules.auth.repository import AuthRepository
from long_invest.modules.auth.session_policy import SessionPolicy
from long_invest.modules.auth.validation import validate_new_password
from long_invest.platform.errors import AppError


class AccountAdminService:
    def __init__(
        self,
        repository: AuthRepository,
        passwords: PasswordService,
        audit: AuthAuditPort,
        audit_context: AuditContext,
    ) -> None:
        self._repository = repository
        self._passwords = passwords
        self._audit = audit
        self._audit_context = audit_context
        self._sessions = SessionPolicy()

    async def create_admin(
        self,
        username: str,
        password: str,
        *,
        now: datetime,
    ) -> AppUser:
        validate_new_password(password)
        if await self._repository.has_any_user():
            await self._record_audit(
                action_code="AUTH_ADMIN_CREATE",
                object_type="app_user",
                object_id=username,
                result="DENIED",
                reason="user_already_exists",
            )
            raise AppError(
                code="AUTH_USER_ALREADY_EXISTS",
                message="管理员账号已存在",
                status_code=409,
            )
        user = AppUser(
            id=uuid4(),
            username=username,
            password_hash=self._passwords.hash(password),
            password_version=1,
            status=UserStatus.ACTIVE,
            created_at=now,
            password_changed_at=now,
        )
        await self._repository.add_user(user)
        await self._record_audit(
            action_code="AUTH_ADMIN_CREATE",
            object_type="app_user",
            object_id=str(user.id),
            result="SUCCESS",
            after_summary={"status": user.status, "password_version": 1},
        )
        await self._repository.flush()
        return user

    async def reset_password(
        self,
        username: str,
        new_password: str,
        *,
        now: datetime,
    ) -> AppUser:
        validate_new_password(new_password)
        user = await self._require_user(username)
        user.password_hash = self._passwords.hash(new_password)
        user.password_version += 1
        user.password_changed_at = now
        await self._invalidate_active_sessions(
            user,
            now=now,
            status=SessionStatus.PASSWORD_CHANGED,
            reason="password reset by CLI",
        )
        await self._record_audit(
            action_code="AUTH_ADMIN_PASSWORD_RESET",
            object_type="app_user",
            object_id=str(user.id),
            result="SUCCESS",
            before_summary={"password_version": user.password_version - 1},
            after_summary={"password_version": user.password_version},
        )
        await self._repository.flush()
        return user

    async def revoke_sessions(self, username: str, *, now: datetime) -> int:
        user = await self._require_user(username)
        changed = await self._invalidate_active_sessions(
            user,
            now=now,
            status=SessionStatus.REVOKED,
            reason="revoked by CLI",
        )
        await self._record_audit(
            action_code="AUTH_ADMIN_SESSIONS_REVOKE",
            object_type="app_user",
            object_id=str(user.id),
            result="SUCCESS" if changed else "NOOP",
            after_summary={"revoked_count": changed},
        )
        await self._repository.flush()
        return changed

    async def disable(self, username: str, *, now: datetime) -> bool:
        user = await self._require_user(username)
        if user.status == UserStatus.DISABLED:
            await self._record_audit(
                action_code="AUTH_ADMIN_DISABLE",
                object_type="app_user",
                object_id=str(user.id),
                result="NOOP",
            )
            return False
        user.status = UserStatus.DISABLED
        await self._invalidate_active_sessions(
            user,
            now=now,
            status=SessionStatus.USER_DISABLED,
            reason="user disabled by CLI",
        )
        await self._record_audit(
            action_code="AUTH_ADMIN_DISABLE",
            object_type="app_user",
            object_id=str(user.id),
            result="SUCCESS",
            before_summary={"status": UserStatus.ACTIVE},
            after_summary={"status": UserStatus.DISABLED},
        )
        await self._repository.flush()
        return True

    async def enable(self, username: str, *, now: datetime) -> bool:
        user = await self._require_user(username)
        if user.status == UserStatus.ACTIVE:
            await self._record_audit(
                action_code="AUTH_ADMIN_ENABLE",
                object_type="app_user",
                object_id=str(user.id),
                result="NOOP",
            )
            return False
        user.status = UserStatus.ACTIVE
        await self._record_audit(
            action_code="AUTH_ADMIN_ENABLE",
            object_type="app_user",
            object_id=str(user.id),
            result="SUCCESS",
            before_summary={"status": UserStatus.DISABLED},
            after_summary={"status": UserStatus.ACTIVE},
        )
        await self._repository.flush()
        return True

    async def _require_user(self, username: str) -> AppUser:
        user = await self._repository.find_user_by_username(username)
        if user is None:
            raise AppError(
                code="AUTH_USER_NOT_FOUND",
                message="管理员账号不存在",
                status_code=404,
            )
        return user

    async def _invalidate_active_sessions(
        self,
        user: AppUser,
        *,
        now: datetime,
        status: SessionStatus,
        reason: str,
    ) -> int:
        changed = 0
        for session in await self._repository.list_sessions(user.id):
            if self._sessions.revoke(
                session,
                now=now,
                reason=reason,
                status=status,
            ):
                changed += 1
        return changed

    async def _record_audit(
        self,
        *,
        action_code: str,
        object_type: str,
        object_id: str,
        result: str,
        reason: str | None = None,
        before_summary: dict[str, object] | None = None,
        after_summary: dict[str, object] | None = None,
    ) -> None:
        await self._audit.record(
            build_auth_audit_event(
                self._audit_context,
                action_code=action_code,
                object_type=object_type,
                object_id=object_id,
                result=result,
                risk_level="CRITICAL",
                reason=reason,
                before_summary=before_summary,
                after_summary=after_summary,
            )
        )
