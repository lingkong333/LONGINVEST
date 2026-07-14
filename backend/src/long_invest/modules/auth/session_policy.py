from datetime import datetime, timedelta
from uuid import uuid4

from long_invest.modules.auth.contracts import (
    RequestActivity,
    SessionStatus,
    UserStatus,
)
from long_invest.modules.auth.models import AppUser, UserSession


class SessionPolicy:
    IDLE_LIFETIME = timedelta(days=30)
    ABSOLUTE_LIFETIME = timedelta(days=90)

    def new_session(
        self,
        *,
        user: AppUser,
        token_digest: str,
        csrf_digest: str,
        now: datetime,
        client_ip: str | None,
        user_agent_summary: str | None,
    ) -> UserSession:
        return UserSession(
            id=uuid4(),
            user_id=user.id,
            token_digest=token_digest,
            csrf_secret_digest=csrf_digest,
            password_version=user.password_version,
            created_at=now,
            last_request_at=now,
            last_user_activity_at=now,
            idle_expires_at=now + self.IDLE_LIFETIME,
            absolute_expires_at=now + self.ABSOLUTE_LIFETIME,
            last_ip=client_ip,
            user_agent_summary=user_agent_summary,
            status=SessionStatus.ACTIVE,
        )

    def record_request(
        self,
        session: UserSession,
        user: AppUser,
        *,
        now: datetime,
        activity: RequestActivity,
        client_ip: str | None = None,
    ) -> SessionStatus:
        current = SessionStatus(session.status)
        if current is not SessionStatus.ACTIVE:
            return current
        if user.status != UserStatus.ACTIVE:
            session.status = SessionStatus.USER_DISABLED
            return SessionStatus.USER_DISABLED
        if session.password_version != user.password_version:
            session.status = SessionStatus.PASSWORD_CHANGED
            return SessionStatus.PASSWORD_CHANGED
        if now >= session.absolute_expires_at:
            session.status = SessionStatus.EXPIRED_ABSOLUTE
            return SessionStatus.EXPIRED_ABSOLUTE
        if now >= session.idle_expires_at:
            session.status = SessionStatus.EXPIRED_IDLE
            return SessionStatus.EXPIRED_IDLE

        session.last_request_at = now
        if client_ip is not None:
            session.last_ip = client_ip
        if activity in {RequestActivity.USER, RequestActivity.WRITE}:
            session.last_user_activity_at = now
            session.idle_expires_at = now + self.IDLE_LIFETIME
        return SessionStatus.ACTIVE

    @staticmethod
    def revoke(
        session: UserSession,
        *,
        now: datetime,
        reason: str,
        status: SessionStatus = SessionStatus.REVOKED,
    ) -> bool:
        if session.status != SessionStatus.ACTIVE:
            return False
        session.status = status
        session.revoked_at = now
        session.revoked_reason = reason
        return True
