from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher

from long_invest.modules.auth.account_service import AccountAdminService
from long_invest.modules.auth.contracts import (
    RequestActivity,
    SessionStatus,
    UserStatus,
)
from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.modules.auth.passwords import PasswordService
from long_invest.modules.auth.rate_limit import RateLimitDecision
from long_invest.modules.auth.service import AuthService
from long_invest.modules.auth.tokens import TokenService
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
PASSWORD = "a sufficiently long password"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class MemoryAuthRepository:
    def __init__(self) -> None:
        self.users: dict[UUID, AppUser] = {}
        self.sessions: dict[UUID, UserSession] = {}

    async def find_user_by_username(self, username: str) -> AppUser | None:
        return next((u for u in self.users.values() if u.username == username), None)

    async def has_any_user(self) -> bool:
        return bool(self.users)

    async def get_user(self, user_id: UUID) -> AppUser | None:
        return self.users.get(user_id)

    async def add_user(self, user: AppUser) -> AppUser:
        self.users[user.id] = user
        return user

    async def find_session_by_digest(self, digest: str) -> UserSession | None:
        return next(
            (s for s in self.sessions.values() if s.token_digest == digest),
            None,
        )

    async def get_session(self, session_id: UUID) -> UserSession | None:
        return self.sessions.get(session_id)

    async def list_sessions(self, user_id: UUID) -> list[UserSession]:
        return [s for s in self.sessions.values() if s.user_id == user_id]

    async def add_session(self, session: UserSession) -> UserSession:
        self.sessions[session.id] = session
        return session

    async def flush(self) -> None:
        return None


class AllowLimiter:
    def check(self, *, ip: str, username: str, now: datetime) -> RateLimitDecision:
        return RateLimitDecision(allowed=True)

    def record_failure(self, *, ip: str, username: str, now: datetime) -> None:
        return None

    def record_success(self, *, ip: str, username: str, now: datetime) -> None:
        return None


class DenyLimiter(AllowLimiter):
    def check(self, *, ip: str, username: str, now: datetime) -> RateLimitDecision:
        return RateLimitDecision(allowed=False, retry_after_seconds=30)


class RecordingPasswordService(PasswordService):
    def __init__(self) -> None:
        super().__init__()
        self.verified_hashes: list[str] = []

    def verify(self, password: str, encoded: str):  # type: ignore[no-untyped-def]
        self.verified_hashes.append(encoded)
        return super().verify(password, encoded)


def make_user(passwords: PasswordService, *, username: str = "admin") -> AppUser:
    return AppUser(
        id=uuid4(),
        username=username,
        password_hash=passwords.hash(PASSWORD),
        password_version=1,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        password_changed_at=NOW,
    )


def make_session(
    user: AppUser,
    *,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> UserSession:
    return UserSession(
        id=uuid4(),
        user_id=user.id,
        token_digest=TokenService.digest(f"token-{uuid4()}"),
        csrf_secret_digest=TokenService.digest(f"csrf-{uuid4()}"),
        password_version=user.password_version,
        created_at=NOW,
        last_request_at=NOW,
        last_user_activity_at=NOW,
        idle_expires_at=NOW + timedelta(days=30),
        absolute_expires_at=NOW + timedelta(days=90),
        status=status,
    )


@pytest.mark.anyio
async def test_login_with_correct_password_creates_fresh_hashed_session() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    await repo.add_user(user)
    service = AuthService(repo, passwords, TokenService(), AllowLimiter())

    result = await service.login(
        username="admin",
        password=PASSWORD,
        client_ip="203.0.113.1",
        user_agent_summary="Browser",
        now=NOW,
    )

    assert result.session.id is not None
    assert result.session.id in repo.sessions
    assert result.session.token_digest == TokenService.digest(
        result.credentials.session_token
    )
    assert result.session.csrf_secret_digest == TokenService.digest(
        result.credentials.csrf_token
    )
    assert result.credentials.session_token not in result.session.__dict__.values()
    assert result.credentials.csrf_token not in result.session.__dict__.values()
    assert user.last_login_at == NOW
    assert user.last_login_ip == "203.0.113.1"


@pytest.mark.anyio
async def test_wrong_and_unknown_users_get_same_credentials_error() -> None:
    repo = MemoryAuthRepository()
    passwords = RecordingPasswordService()
    await repo.add_user(make_user(passwords))
    service = AuthService(repo, passwords, TokenService(), AllowLimiter())

    for username in ("admin", "missing"):
        with pytest.raises(AppError) as captured:
            await service.login(
                username=username,
                password="wrong password value",
                client_ip="203.0.113.1",
                user_agent_summary="Browser",
                now=NOW,
            )
        assert captured.value.code == "AUTH_INVALID_CREDENTIALS"
        assert captured.value.status_code == 401
        assert captured.value.message == "用户名或密码错误"

    assert len(passwords.verified_hashes) == 2
    assert all(
        encoded.startswith("$argon2id$") for encoded in passwords.verified_hashes
    )


@pytest.mark.anyio
async def test_disabled_user_gets_the_same_credentials_error() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    user.status = UserStatus.DISABLED
    await repo.add_user(user)

    with pytest.raises(AppError) as captured:
        await AuthService(repo, passwords, TokenService(), AllowLimiter()).login(
            username="admin",
            password=PASSWORD,
            client_ip="203.0.113.1",
            user_agent_summary="Browser",
            now=NOW,
        )

    assert captured.value.code == "AUTH_INVALID_CREDENTIALS"
    assert captured.value.status_code == 401


@pytest.mark.anyio
async def test_login_upgrades_old_hash_without_changing_password_version() -> None:
    repo = MemoryAuthRepository()
    old = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    current = PasswordService(
        PasswordHasher(time_cost=2, memory_cost=8192, parallelism=1)
    )
    user = AppUser(
        id=uuid4(),
        username="admin",
        password_hash=old.hash(PASSWORD),
        password_version=7,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        password_changed_at=NOW,
    )
    await repo.add_user(user)

    await AuthService(repo, current, TokenService(), AllowLimiter()).login(
        username="admin",
        password=PASSWORD,
        client_ip="203.0.113.1",
        user_agent_summary="Browser",
        now=NOW,
    )

    assert current.verify(PASSWORD, user.password_hash).valid is True
    assert old.check_needs_rehash(user.password_hash) is True
    assert user.password_version == 7


@pytest.mark.anyio
async def test_login_rate_limit_is_reported_before_credentials_check() -> None:
    repo = MemoryAuthRepository()

    with pytest.raises(AppError) as captured:
        await AuthService(
            repo,
            PasswordService(),
            TokenService(),
            DenyLimiter(),
        ).login(
            username="admin",
            password=PASSWORD,
            client_ip="203.0.113.1",
            user_agent_summary="Browser",
            now=NOW,
        )

    assert captured.value.code == "AUTH_RATE_LIMITED"
    assert captured.value.status_code == 429
    assert captured.value.details == {"retry_after_seconds": 30}


@pytest.mark.anyio
async def test_authenticate_records_background_without_extending_idle() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    await repo.add_user(user)
    auth = AuthService(repo, passwords, TokenService(), AllowLimiter())
    login = await auth.login(
        username="admin",
        password=PASSWORD,
        client_ip="203.0.113.1",
        user_agent_summary="Browser",
        now=NOW,
    )
    original_idle = login.session.idle_expires_at

    authenticated = await auth.authenticate(
        token=login.credentials.session_token,
        now=NOW + timedelta(hours=1),
        activity=RequestActivity.BACKGROUND,
        client_ip="203.0.113.1",
    )

    assert authenticated.session.id == login.session.id
    assert authenticated.session.last_request_at == NOW + timedelta(hours=1)
    assert authenticated.session.last_user_activity_at == NOW
    assert authenticated.session.idle_expires_at == original_idle


@pytest.mark.anyio
async def test_revoke_session_is_idempotent() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    session = make_session(user)
    await repo.add_user(user)
    await repo.add_session(session)
    auth = AuthService(repo, passwords, TokenService(), AllowLimiter())

    first = await auth.revoke_session(
        user_id=user.id,
        session_id=session.id,
        now=NOW,
        reason="user request",
    )
    second = await auth.revoke_session(
        user_id=user.id,
        session_id=session.id,
        now=NOW + timedelta(seconds=1),
        reason="replayed request",
    )

    assert first is True
    assert second is False
    assert session.revoked_reason == "user request"


@pytest.mark.anyio
async def test_revoke_other_sessions_keeps_current_session_active() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    current = make_session(user)
    other = make_session(user)
    await repo.add_user(user)
    await repo.add_session(current)
    await repo.add_session(other)
    auth = AuthService(repo, passwords, TokenService(), AllowLimiter())

    assert (
        await auth.revoke_other_sessions(
            user_id=user.id,
            current_session_id=current.id,
            now=NOW,
            reason="revoke others",
        )
        == 1
    )
    assert (
        await auth.revoke_other_sessions(
            user_id=user.id,
            current_session_id=current.id,
            now=NOW,
            reason="replayed",
        )
        == 0
    )
    assert current.status == SessionStatus.ACTIVE
    assert other.status == SessionStatus.REVOKED


@pytest.mark.anyio
async def test_change_password_rotates_and_invalidates_old_sessions() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    current = make_session(user)
    other = make_session(user)
    await repo.add_user(user)
    await repo.add_session(current)
    await repo.add_session(other)
    auth = AuthService(repo, passwords, TokenService(), AllowLimiter())

    rotated = await auth.change_password(
        user_id=user.id,
        current_session_id=current.id,
        new_password="a different long password",
        confirmation="a different long password",
        client_ip="203.0.113.2",
        user_agent_summary="Browser",
        now=NOW + timedelta(days=1),
    )

    assert user.password_version == 2
    assert current.status == SessionStatus.PASSWORD_CHANGED
    assert other.status == SessionStatus.PASSWORD_CHANGED
    assert rotated.session.status == SessionStatus.ACTIVE
    assert rotated.session.password_version == 2
    assert rotated.session.id not in {current.id, other.id}
    assert (
        passwords.verify("a different long password", user.password_hash).valid is True
    )


@pytest.mark.anyio
async def test_cli_create_admin_rejects_duplicate_and_validates_password() -> None:
    repo = MemoryAuthRepository()
    service = AccountAdminService(repo, PasswordService())
    created = await service.create_admin("admin", PASSWORD, now=NOW)

    assert created.username == "admin"
    assert created.status == UserStatus.ACTIVE
    with pytest.raises(AppError) as duplicate:
        await service.create_admin("admin", PASSWORD, now=NOW)
    assert duplicate.value.code == "AUTH_USER_ALREADY_EXISTS"
    with pytest.raises(AppError) as second_admin:
        await service.create_admin("other", PASSWORD, now=NOW)
    assert second_admin.value.code == "AUTH_USER_ALREADY_EXISTS"
    with pytest.raises(AppError) as short:
        await service.create_admin("other", "too short", now=NOW)
    assert short.value.code == "AUTH_PASSWORD_INVALID"


@pytest.mark.anyio
async def test_cli_password_reset_invalidates_all_sessions() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    first = make_session(user)
    second = make_session(user)
    await repo.add_user(user)
    await repo.add_session(first)
    await repo.add_session(second)

    changed = await AccountAdminService(repo, passwords).reset_password(
        "admin",
        "a different long password",
        now=NOW + timedelta(days=1),
    )

    assert changed.password_version == 2
    assert passwords.verify("a different long password", changed.password_hash).valid
    assert first.status == SessionStatus.PASSWORD_CHANGED
    assert second.status == SessionStatus.PASSWORD_CHANGED


@pytest.mark.anyio
async def test_cli_revoke_disable_and_enable_are_idempotent() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    first = make_session(user)
    second = make_session(user)
    await repo.add_user(user)
    await repo.add_session(first)
    await repo.add_session(second)
    service = AccountAdminService(repo, passwords)

    assert await service.revoke_sessions("admin", now=NOW) == 2
    assert await service.revoke_sessions("admin", now=NOW) == 0
    active_after_revoke = make_session(user)
    await repo.add_session(active_after_revoke)
    assert await service.disable("admin", now=NOW) is True
    assert await service.disable("admin", now=NOW) is False
    assert await service.enable("admin", now=NOW) is True
    assert await service.enable("admin", now=NOW) is False
    assert first.status == SessionStatus.REVOKED
    assert second.status == SessionStatus.REVOKED
    assert active_after_revoke.status == SessionStatus.USER_DISABLED
