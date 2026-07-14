from datetime import UTC, datetime, timedelta
from uuid import uuid4

from long_invest.modules.auth.contracts import (
    RequestActivity,
    SessionStatus,
    UserStatus,
)
from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.modules.auth.session_policy import SessionPolicy

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def make_user(*, password_version: int = 1) -> AppUser:
    return AppUser(
        id=uuid4(),
        username="admin",
        password_hash="encoded",
        password_version=password_version,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        password_changed_at=NOW,
    )


def make_session(
    user: AppUser,
    *,
    idle_expires_at: datetime | None = None,
    absolute_expires_at: datetime | None = None,
    password_version: int | None = None,
) -> UserSession:
    return UserSession(
        id=uuid4(),
        user_id=user.id,
        token_digest="a" * 64,
        csrf_secret_digest="b" * 64,
        password_version=password_version or user.password_version,
        created_at=NOW,
        last_request_at=NOW,
        last_user_activity_at=NOW,
        idle_expires_at=idle_expires_at or NOW + timedelta(days=30),
        absolute_expires_at=absolute_expires_at or NOW + timedelta(days=90),
        status=SessionStatus.ACTIVE,
    )


def test_new_session_has_30_day_idle_and_90_day_absolute_deadlines() -> None:
    user = make_user()
    session = SessionPolicy().new_session(
        user=user,
        token_digest="a" * 64,
        csrf_digest="b" * 64,
        now=NOW,
        client_ip="203.0.113.1",
        user_agent_summary="Browser",
    )

    assert session.idle_expires_at == NOW + timedelta(days=30)
    assert session.absolute_expires_at == NOW + timedelta(days=90)
    assert session.last_user_activity_at == NOW
    assert session.last_request_at == NOW


def test_idle_session_is_active_before_boundary_and_expires_at_boundary() -> None:
    user = make_user()
    policy = SessionPolicy()
    active = make_session(user)
    expired = make_session(user)

    assert (
        policy.record_request(
            active,
            user,
            now=NOW + timedelta(days=30) - timedelta(microseconds=1),
            activity=RequestActivity.BACKGROUND,
        )
        == SessionStatus.ACTIVE
    )
    assert (
        policy.record_request(
            expired,
            user,
            now=NOW + timedelta(days=30),
            activity=RequestActivity.USER,
        )
        == SessionStatus.EXPIRED_IDLE
    )
    assert expired.last_user_activity_at == NOW


def test_absolute_deadline_wins_even_when_idle_deadline_is_later() -> None:
    user = make_user()
    session = make_session(
        user,
        idle_expires_at=NOW + timedelta(days=100),
    )

    status = SessionPolicy().record_request(
        session,
        user,
        now=NOW + timedelta(days=90),
        activity=RequestActivity.USER,
    )

    assert status == SessionStatus.EXPIRED_ABSOLUTE
    assert session.absolute_expires_at == NOW + timedelta(days=90)


def test_background_request_does_not_extend_user_activity() -> None:
    user = make_user()
    session = make_session(user)
    original_idle_deadline = session.idle_expires_at
    request_at = NOW + timedelta(days=1)

    status = SessionPolicy().record_request(
        session,
        user,
        now=request_at,
        activity=RequestActivity.BACKGROUND,
    )

    assert status == SessionStatus.ACTIVE
    assert session.last_request_at == request_at
    assert session.last_user_activity_at == NOW
    assert session.idle_expires_at == original_idle_deadline


def test_real_activity_and_write_extend_idle_but_not_absolute_deadline() -> None:
    user = make_user()
    policy = SessionPolicy()
    user_session = make_session(user)
    write_session = make_session(user)
    absolute_deadline = user_session.absolute_expires_at

    for session, activity in (
        (user_session, RequestActivity.USER),
        (write_session, RequestActivity.WRITE),
    ):
        request_at = NOW + timedelta(days=1)
        status = policy.record_request(
            session,
            user,
            now=request_at,
            activity=activity,
        )
        assert status == SessionStatus.ACTIVE
        assert session.last_user_activity_at == request_at
        assert session.idle_expires_at == request_at + timedelta(days=30)
        assert session.absolute_expires_at == absolute_deadline


def test_password_version_and_disabled_user_invalidate_active_session() -> None:
    policy = SessionPolicy()
    changed_user = make_user(password_version=2)
    stale_session = make_session(changed_user, password_version=1)
    disabled_user = make_user()
    disabled_user.status = UserStatus.DISABLED
    disabled_session = make_session(disabled_user)

    assert (
        policy.record_request(
            stale_session,
            changed_user,
            now=NOW,
            activity=RequestActivity.BACKGROUND,
        )
        == SessionStatus.PASSWORD_CHANGED
    )
    assert (
        policy.record_request(
            disabled_session,
            disabled_user,
            now=NOW,
            activity=RequestActivity.BACKGROUND,
        )
        == SessionStatus.USER_DISABLED
    )


def test_revocation_is_idempotent_and_cannot_be_revived() -> None:
    user = make_user()
    session = make_session(user)
    policy = SessionPolicy()

    assert policy.revoke(session, now=NOW, reason="user request") is True
    replayed = policy.revoke(
        session,
        now=NOW + timedelta(seconds=1),
        reason="again",
    )
    assert replayed is False
    assert session.revoked_at == NOW
    assert session.revoked_reason == "user request"
    assert (
        policy.record_request(
            session,
            user,
            now=NOW + timedelta(days=1),
            activity=RequestActivity.USER,
        )
        == SessionStatus.REVOKED
    )
