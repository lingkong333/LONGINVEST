from datetime import UTC, datetime
from uuid import uuid4

from long_invest.modules.auth.contracts import SessionStatus, UserStatus
from long_invest.modules.auth.models import AppUser, UserSession


def test_user_and_session_models_define_the_owned_tables() -> None:
    assert AppUser.__tablename__ == "app_user"
    assert UserSession.__tablename__ == "user_session"
    assert set(AppUser.__table__.columns.keys()) == {
        "id",
        "username",
        "password_hash",
        "password_version",
        "status",
        "created_at",
        "password_changed_at",
        "last_login_at",
        "last_login_ip",
    }
    assert set(UserSession.__table__.columns.keys()) == {
        "id",
        "user_id",
        "token_digest",
        "csrf_secret_digest",
        "password_version",
        "created_at",
        "last_request_at",
        "last_user_activity_at",
        "idle_expires_at",
        "absolute_expires_at",
        "last_ip",
        "user_agent_summary",
        "status",
        "revoked_at",
        "revoked_reason",
    }


def test_session_model_has_no_plaintext_token_fields() -> None:
    column_names = set(UserSession.__table__.columns.keys())

    assert "token" not in column_names
    assert "csrf_token" not in column_names
    assert "session_token" not in column_names


def test_model_defaults_match_active_first_version_account() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    user = AppUser(
        id=uuid4(),
        username="admin",
        password_hash="encoded",
        created_at=now,
        password_changed_at=now,
    )

    assert user.status == UserStatus.ACTIVE
    assert user.password_version == 1

    session = UserSession(
        id=uuid4(),
        user_id=user.id,
        token_digest="a" * 64,
        csrf_secret_digest="b" * 64,
        password_version=1,
        created_at=now,
        last_request_at=now,
        last_user_activity_at=now,
        idle_expires_at=now,
        absolute_expires_at=now,
    )
    assert session.status == SessionStatus.ACTIVE
