import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher

from long_invest.modules.auth.account_service import AccountAdminService
from long_invest.modules.auth.audit import AuditContext, AuthAuditEvent
from long_invest.modules.auth.contracts import (
    RequestActivity,
    SessionStatus,
    UserStatus,
)
from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.modules.auth.passwords import PasswordService
from long_invest.modules.auth.rate_limit import (
    InMemoryLoginRateLimiter,
    RateLimitConfig,
    RateLimitDecision,
)
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

    async def get_user(self, user_id: UUID) -> AppUser | None:
        return self.users.get(user_id)

    async def add_user(self, user: AppUser) -> AppUser:
        self.users[user.id] = user
        return user

    async def add_admin_if_absent(self, user: AppUser) -> bool:
        if self.users:
            return False
        self.users[user.id] = user
        return True

    async def advance_password_version(
        self,
        user_id: UUID,
        *,
        expected_version: int,
        password_hash: str,
        changed_at: datetime,
    ) -> AppUser | None:
        user = self.users.get(user_id)
        if user is None or user.password_version != expected_version:
            return None
        user.password_hash = password_hash
        user.password_version += 1
        user.password_changed_at = changed_at
        return user

    async def replace_password_hash(
        self,
        user_id: UUID,
        *,
        expected_version: int,
        expected_hash: str,
        replacement_hash: str,
    ) -> bool:
        user = self.users.get(user_id)
        if (
            user is None
            or user.password_version != expected_version
            or user.password_hash != expected_hash
        ):
            return False
        user.password_hash = replacement_hash
        return True

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


class ConcurrentChangeRepository(MemoryAuthRepository):
    def __init__(self) -> None:
        super().__init__()
        self._session_reads = 0
        self._both_session_reads = asyncio.Event()
        self._password_update_lock = asyncio.Lock()

    async def get_user(self, user_id: UUID) -> AppUser | None:
        user = self.users.get(user_id)
        if user is None:
            return None
        return AppUser(
            id=user.id,
            username=user.username,
            password_hash=user.password_hash,
            password_version=user.password_version,
            status=user.status,
            created_at=user.created_at,
            password_changed_at=user.password_changed_at,
        )

    async def get_session(self, session_id: UUID) -> UserSession | None:
        session = self.sessions.get(session_id)
        if session is None:
            return None
        snapshot = UserSession(
            id=session.id,
            user_id=session.user_id,
            token_digest=session.token_digest,
            csrf_secret_digest=session.csrf_secret_digest,
            password_version=session.password_version,
            created_at=session.created_at,
            last_request_at=session.last_request_at,
            last_user_activity_at=session.last_user_activity_at,
            idle_expires_at=session.idle_expires_at,
            absolute_expires_at=session.absolute_expires_at,
            status=session.status,
        )
        self._session_reads += 1
        if self._session_reads == 2:
            self._both_session_reads.set()
        await self._both_session_reads.wait()
        return snapshot

    async def advance_password_version(
        self,
        user_id: UUID,
        *,
        expected_version: int,
        password_hash: str,
        changed_at: datetime,
    ) -> AppUser | None:
        async with self._password_update_lock:
            return await super().advance_password_version(
                user_id,
                expected_version=expected_version,
                password_hash=password_hash,
                changed_at=changed_at,
            )


class HashUpgradeRaceRepository(MemoryAuthRepository):
    def __init__(self, concurrent_hash: str) -> None:
        super().__init__()
        self._concurrent_hash = concurrent_hash

    async def replace_password_hash(
        self,
        user_id: UUID,
        *,
        expected_version: int,
        expected_hash: str,
        replacement_hash: str,
    ) -> bool:
        user = self.users[user_id]
        user.password_hash = self._concurrent_hash
        user.password_version += 1
        return False

    async def add_session(self, session: UserSession) -> UserSession:
        user = self.users[session.user_id]
        user.password_hash = self._concurrent_hash
        user.password_version += 1
        return await super().add_session(session)


class FailingLookupRepository(MemoryAuthRepository):
    async def find_user_by_username(self, username: str) -> AppUser | None:
        raise RuntimeError("database unavailable")


class ConcurrentAdminRepository(MemoryAuthRepository):
    def __init__(self) -> None:
        super().__init__()
        self._create_lock = asyncio.Lock()

    async def add_admin_if_absent(self, user: AppUser) -> bool:
        async with self._create_lock:
            return await super().add_admin_if_absent(user)


class AllowLimiter:
    async def check(
        self, *, ip: str, username: str, now: datetime
    ) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, reservation_id="reservation")

    async def record_failure(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        return None

    async def record_success(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        return None


class DenyLimiter(AllowLimiter):
    async def check(
        self, *, ip: str, username: str, now: datetime
    ) -> RateLimitDecision:
        return RateLimitDecision(allowed=False, retry_after_seconds=30)


class RecordingAuditPort:
    def __init__(self) -> None:
        self.events: list[AuthAuditEvent] = []

    async def record(self, event: AuthAuditEvent) -> None:
        self.events.append(event)


def audit_context() -> AuditContext:
    unique = uuid4().hex
    return AuditContext(
        request_id=f"req_{unique}",
        idempotency_key=f"audit_{unique}",
        trusted_ip="203.0.113.1",
    )


def make_auth_service(
    repo: MemoryAuthRepository,
    passwords: PasswordService,
    *,
    limiter: AllowLimiter | None = None,
    audit: RecordingAuditPort | None = None,
    context: AuditContext | None = None,
) -> AuthService:
    return AuthService(
        repo,
        passwords,
        TokenService(),
        limiter or AllowLimiter(),
        audit or RecordingAuditPort(),
        context or audit_context(),
        dummy_password_hash=passwords.hash("startup generated dummy password"),
    )


def make_admin_service(
    repo: MemoryAuthRepository,
    passwords: PasswordService,
    *,
    audit: RecordingAuditPort | None = None,
    context: AuditContext | None = None,
) -> AccountAdminService:
    return AccountAdminService(
        repo,
        passwords,
        audit or RecordingAuditPort(),
        context or audit_context(),
    )


class RecordingPasswordService(PasswordService):
    def __init__(self) -> None:
        super().__init__()
        self.verified_hashes: list[str] = []

    def verify(self, password: str, encoded: str):  # type: ignore[no-untyped-def]
        self.verified_hashes.append(encoded)
        return super().verify(password, encoded)


class CountingPasswordService(PasswordService):
    def __init__(self) -> None:
        super().__init__()
        self.hash_calls = 0

    def hash(self, password: str) -> str:
        self.hash_calls += 1
        return super().hash(password)


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


def test_auth_service_constructor_does_not_hash_a_dummy_password() -> None:
    repo = MemoryAuthRepository()
    passwords = CountingPasswordService()
    dummy_hash = passwords.hash("startup generated dummy password")
    calls_before_construction = passwords.hash_calls

    AuthService(
        repo,
        passwords,
        TokenService(),
        AllowLimiter(),
        RecordingAuditPort(),
        audit_context(),
        dummy_password_hash=dummy_hash,
    )

    assert passwords.hash_calls == calls_before_construction


@pytest.mark.anyio
async def test_login_with_correct_password_creates_fresh_hashed_session() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    await repo.add_user(user)
    audit = RecordingAuditPort()
    context = audit_context()
    service = make_auth_service(repo, passwords, audit=audit, context=context)

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
    assert [(event.action_code, event.result) for event in audit.events] == [
        ("AUTH_LOGIN", "SUCCESS")
    ]
    assert audit.events[0].request_id == context.request_id
    assert audit.events[0].session_id == str(result.session.id)


@pytest.mark.anyio
async def test_wrong_and_unknown_users_get_same_credentials_error() -> None:
    repo = MemoryAuthRepository()
    passwords = RecordingPasswordService()
    await repo.add_user(make_user(passwords))
    audit = RecordingAuditPort()
    context = audit_context()
    service = make_auth_service(repo, passwords, audit=audit, context=context)

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
    assert [event.result for event in audit.events] == ["FAILED", "FAILED"]
    assert all(event.action_code == "AUTH_LOGIN" for event in audit.events)
    assert all(event.request_id == context.request_id for event in audit.events)
    assert all(event.idempotency_key for event in audit.events)
    assert PASSWORD not in repr(audit.events)


@pytest.mark.anyio
async def test_backend_failure_releases_the_atomic_login_reservation() -> None:
    limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=1, per_username=1, global_failures=1)
    )
    passwords = PasswordService()
    auth = AuthService(
        FailingLookupRepository(),
        passwords,
        TokenService(),
        limiter,
        RecordingAuditPort(),
        audit_context(),
        dummy_password_hash=passwords.hash("startup dummy password"),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await auth.login(
            username="admin",
            password=PASSWORD,
            client_ip="203.0.113.1",
            user_agent_summary="Browser",
            now=NOW,
        )

    assert (await limiter.check(ip="203.0.113.1", username="admin", now=NOW)).allowed


@pytest.mark.anyio
async def test_disabled_user_gets_the_same_credentials_error() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    user.status = UserStatus.DISABLED
    await repo.add_user(user)

    with pytest.raises(AppError) as captured:
        await make_auth_service(repo, passwords).login(
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

    await make_auth_service(repo, current).login(
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
async def test_hash_upgrade_cannot_overwrite_a_concurrent_password_change() -> None:
    old_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    passwords = PasswordService(
        PasswordHasher(time_cost=2, memory_cost=8192, parallelism=1)
    )
    concurrent_hash = passwords.hash("concurrently changed password")
    repo = HashUpgradeRaceRepository(concurrent_hash)
    user = AppUser(
        id=uuid4(),
        username="admin",
        password_hash=old_hasher.hash(PASSWORD),
        password_version=1,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        password_changed_at=NOW,
    )
    await repo.add_user(user)

    with pytest.raises(AppError) as captured:
        await make_auth_service(repo, passwords).login(
            username="admin",
            password=PASSWORD,
            client_ip="203.0.113.1",
            user_agent_summary="Browser",
            now=NOW + timedelta(minutes=1),
        )

    assert captured.value.code == "AUTH_INVALID_CREDENTIALS"
    assert repo.users[user.id].password_hash == concurrent_hash
    assert repo.users[user.id].password_version == 2
    assert repo.sessions == {}


@pytest.mark.anyio
async def test_login_rate_limit_is_reported_before_credentials_check() -> None:
    repo = MemoryAuthRepository()
    audit = RecordingAuditPort()
    context = audit_context()

    with pytest.raises(AppError) as captured:
        await make_auth_service(
            repo,
            PasswordService(),
            limiter=DenyLimiter(),
            audit=audit,
            context=context,
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
    assert [(event.action_code, event.result) for event in audit.events] == [
        ("AUTH_LOGIN", "DENIED")
    ]


@pytest.mark.anyio
async def test_authenticate_records_background_without_extending_idle() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    await repo.add_user(user)
    auth = make_auth_service(repo, passwords)
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
async def test_csrf_can_be_reissued_and_is_bound_to_its_session() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    first_user = make_user(passwords)
    second_user = make_user(passwords, username="second")
    await repo.add_user(first_user)
    await repo.add_user(second_user)
    auth = make_auth_service(repo, passwords)
    first = await auth.login(
        username="admin",
        password=PASSWORD,
        client_ip="203.0.113.1",
        user_agent_summary="Browser",
        now=NOW,
    )
    second = await auth.login(
        username="second",
        password=PASSWORD,
        client_ip="203.0.113.2",
        user_agent_summary="Browser",
        now=NOW,
    )
    old_csrf = first.credentials.csrf_token

    refreshed = await auth.issue_csrf(
        session_token=first.credentials.session_token,
        now=NOW + timedelta(minutes=1),
        client_ip="203.0.113.1",
    )

    assert refreshed.csrf_token != old_csrf
    assert refreshed.csrf_token not in first.session.__dict__.values()
    with pytest.raises(AppError) as old_token:
        await auth.validate_csrf(
            session_token=first.credentials.session_token,
            csrf_token=old_csrf,
            now=NOW + timedelta(minutes=2),
            client_ip="203.0.113.1",
        )
    assert old_token.value.code == "AUTH_CSRF_INVALID"
    with pytest.raises(AppError) as other_session:
        await auth.validate_csrf(
            session_token=first.credentials.session_token,
            csrf_token=second.credentials.csrf_token,
            now=NOW + timedelta(minutes=2),
            client_ip="203.0.113.1",
        )
    assert other_session.value.code == "AUTH_CSRF_INVALID"
    validated = await auth.validate_csrf(
        session_token=first.credentials.session_token,
        csrf_token=refreshed.csrf_token,
        now=NOW + timedelta(minutes=2),
        client_ip="203.0.113.1",
    )
    assert validated.session.id == first.session.id


@pytest.mark.anyio
async def test_replay_credentials_accept_only_the_original_revoked_session() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    session_token = "original-session-token"
    csrf_token = "original-csrf-token"
    session = UserSession(
        id=uuid4(),
        user_id=user.id,
        token_digest=TokenService.digest(session_token),
        csrf_secret_digest=TokenService.digest(csrf_token),
        password_version=user.password_version,
        created_at=NOW,
        last_request_at=NOW,
        last_user_activity_at=NOW,
        idle_expires_at=NOW + timedelta(days=30),
        absolute_expires_at=NOW + timedelta(days=90),
        status=SessionStatus.REVOKED,
        revoked_at=NOW,
        revoked_reason="user logout",
    )
    await repo.add_user(user)
    await repo.add_session(session)
    auth = make_auth_service(repo, passwords)

    validated = await auth.validate_replay_credentials(
        session_token=session_token,
        csrf_token=csrf_token,
        expected_session_id=str(session.id),
    )

    assert validated.id == session.id
    with pytest.raises(AppError) as wrong_csrf:
        await auth.validate_replay_credentials(
            session_token=session_token,
            csrf_token="wrong-csrf",
            expected_session_id=str(session.id),
        )
    assert wrong_csrf.value.code == "AUTH_CSRF_INVALID"
    with pytest.raises(AppError) as wrong_session:
        await auth.validate_replay_credentials(
            session_token=session_token,
            csrf_token=csrf_token,
            expected_session_id=str(uuid4()),
        )
    assert wrong_session.value.code == "AUTH_SESSION_INVALID"
    session.status = SessionStatus.PASSWORD_CHANGED
    with pytest.raises(AppError) as invalidated:
        await auth.validate_replay_credentials(
            session_token=session_token,
            csrf_token=csrf_token,
            expected_session_id=str(session.id),
        )
    assert invalidated.value.code == "AUTH_SESSION_INVALID"


@pytest.mark.anyio
async def test_revoke_session_is_idempotent() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    session = make_session(user)
    await repo.add_user(user)
    await repo.add_session(session)
    audit = RecordingAuditPort()
    context = audit_context()
    auth = make_auth_service(repo, passwords, audit=audit, context=context)

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
    assert [(event.action_code, event.result) for event in audit.events] == [
        ("AUTH_SESSION_REVOKE", "SUCCESS"),
        ("AUTH_SESSION_REVOKE", "NOOP"),
    ]
    assert all(event.request_id == context.request_id for event in audit.events)


@pytest.mark.anyio
async def test_logout_audit_identifies_the_current_session() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    session = make_session(user)
    await repo.add_user(user)
    await repo.add_session(session)
    audit = RecordingAuditPort()
    auth = make_auth_service(repo, passwords, audit=audit)

    await auth.revoke_session(
        user_id=user.id,
        session_id=session.id,
        now=NOW,
        reason="user logout",
        actor_session_id=session.id,
        action_code="AUTH_LOGOUT",
    )

    assert audit.events[0].action_code == "AUTH_LOGOUT"
    assert audit.events[0].session_id == str(session.id)


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
    auth = make_auth_service(repo, passwords)

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
async def test_revoke_all_sessions_includes_current_session() -> None:
    repo = MemoryAuthRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    current = make_session(user)
    other = make_session(user)
    await repo.add_user(user)
    await repo.add_session(current)
    await repo.add_session(other)
    audit = RecordingAuditPort()
    auth = make_auth_service(repo, passwords, audit=audit)

    changed = await auth.revoke_all_sessions(
        user_id=user.id,
        current_session_id=current.id,
        now=NOW,
        reason="revoke all",
    )

    assert changed == 2
    assert current.status == SessionStatus.REVOKED
    assert other.status == SessionStatus.REVOKED
    assert [(event.action_code, event.result) for event in audit.events] == [
        ("AUTH_SESSION_REVOKE_ALL", "SUCCESS")
    ]


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
    audit = RecordingAuditPort()
    context = audit_context()
    auth = make_auth_service(repo, passwords, audit=audit, context=context)

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
    assert [(event.action_code, event.result) for event in audit.events] == [
        ("AUTH_PASSWORD_CHANGE", "SUCCESS")
    ]
    assert audit.events[0].before_summary == {"password_version": 1}
    assert audit.events[0].after_summary == {"password_version": 2}
    assert audit.events[0].request_id == context.request_id


@pytest.mark.anyio
async def test_concurrent_password_changes_allow_only_one_old_request() -> None:
    repo = ConcurrentChangeRepository()
    passwords = PasswordService()
    user = make_user(passwords)
    current = make_session(user)
    await repo.add_user(user)
    await repo.add_session(current)
    auth = make_auth_service(repo, passwords)

    results = await asyncio.gather(
        auth.change_password(
            user_id=user.id,
            current_session_id=current.id,
            new_password="first different password",
            confirmation="first different password",
            client_ip="203.0.113.1",
            user_agent_summary="Browser",
            now=NOW + timedelta(days=1),
        ),
        auth.change_password(
            user_id=user.id,
            current_session_id=current.id,
            new_password="second different password",
            confirmation="second different password",
            client_ip="203.0.113.1",
            user_agent_summary="Browser",
            now=NOW + timedelta(days=1),
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, AppError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "AUTH_SESSION_INVALID"
    assert repo.users[user.id].password_version == 2


@pytest.mark.anyio
async def test_cli_create_admin_rejects_duplicate_and_validates_password() -> None:
    repo = MemoryAuthRepository()
    audit = RecordingAuditPort()
    context = audit_context()
    service = make_admin_service(
        repo,
        PasswordService(),
        audit=audit,
        context=context,
    )
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
    assert audit.events[0].action_code == "AUTH_ADMIN_CREATE"
    assert audit.events[0].result == "SUCCESS"
    assert audit.events[0].request_id == context.request_id


@pytest.mark.anyio
async def test_concurrent_different_admin_names_create_only_one_user() -> None:
    repo = ConcurrentAdminRepository()
    passwords = PasswordService()
    first = make_admin_service(repo, passwords)
    second = make_admin_service(repo, passwords)

    results = await asyncio.gather(
        first.create_admin("first-admin", PASSWORD, now=NOW),
        second.create_admin("second-admin", PASSWORD, now=NOW),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, AppUser)]
    failures = [result for result in results if isinstance(result, AppError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "AUTH_USER_ALREADY_EXISTS"
    assert len(repo.users) == 1


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

    audit = RecordingAuditPort()
    changed = await make_admin_service(
        repo,
        passwords,
        audit=audit,
    ).reset_password(
        "admin",
        "a different long password",
        now=NOW + timedelta(days=1),
    )

    assert changed.password_version == 2
    assert passwords.verify("a different long password", changed.password_hash).valid
    assert first.status == SessionStatus.PASSWORD_CHANGED
    assert second.status == SessionStatus.PASSWORD_CHANGED
    assert [(event.action_code, event.result) for event in audit.events] == [
        ("AUTH_ADMIN_PASSWORD_RESET", "SUCCESS")
    ]


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
    audit = RecordingAuditPort()
    service = make_admin_service(repo, passwords, audit=audit)

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
    assert [event.action_code for event in audit.events] == [
        "AUTH_ADMIN_SESSIONS_REVOKE",
        "AUTH_ADMIN_SESSIONS_REVOKE",
        "AUTH_ADMIN_DISABLE",
        "AUTH_ADMIN_DISABLE",
        "AUTH_ADMIN_ENABLE",
        "AUTH_ADMIN_ENABLE",
    ]
