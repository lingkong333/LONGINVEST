from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.auth.application import (
    AuthApplication,
    _resolve_audit_replay,
    _resolve_request_replay,
)
from long_invest.modules.auth.audit import (
    AuditContext,
    auth_audit_idempotency_key,
    build_auth_audit_event,
)
from long_invest.modules.auth.contracts import SessionStatus, UserStatus
from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.modules.auth.passwords import PasswordService
from long_invest.modules.auth.rate_limit import InMemoryLoginRateLimiter
from long_invest.modules.auth.tokens import TokenService
from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.audit.repository import NewAuditEvent
from long_invest.platform.errors import AppError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _audit_data(*, session_id: str = "session-1") -> NewAuditEvent:
    return NewAuditEvent(
        action_code="AUTH_SESSION_REVOKE",
        object_type="user_session",
        object_id="target-session",
        result="SUCCESS",
        request_id="req_test",
        idempotency_key="auth:test-key",
        risk_level="HIGH",
        reason="user request",
        before_summary={"status": "ACTIVE"},
        after_summary={"status": "REVOKED"},
        actor_user_id="user-1",
        session_id=session_id,
        trusted_ip="203.0.113.1",
    )


def _stored_audit(data: NewAuditEvent) -> AuditEvent:
    return AuditEvent(
        action_code=data.action_code,
        object_type=data.object_type,
        object_id=data.object_id,
        result=data.result,
        request_id=data.request_id,
        idempotency_key=data.idempotency_key,
        risk_level=data.risk_level,
        reason=data.reason,
        before_summary=data.before_summary,
        after_summary=data.after_summary,
        actor_user_id=data.actor_user_id,
        session_id=data.session_id,
        trusted_ip=data.trusted_ip,
    )


def test_audit_replay_accepts_only_identical_content() -> None:
    original = _audit_data()
    stored = _stored_audit(original)

    _resolve_audit_replay(stored, original)
    with pytest.raises(AppError) as caught:
        _resolve_audit_replay(stored, _audit_data(session_id="session-2"))

    assert caught.value.code == "AUTH_AUDIT_IDEMPOTENCY_CONFLICT"
    assert caught.value.status_code == 409


def test_request_idempotency_key_is_shared_across_auth_actions() -> None:
    context = AuditContext("req_test", "same-request-key")
    logout = build_auth_audit_event(
        context,
        action_code="AUTH_LOGOUT",
        object_type="user_session",
        object_id="session-1",
        result="SUCCESS",
        risk_level="HIGH",
    )
    revoke_all = build_auth_audit_event(
        context,
        action_code="AUTH_SESSION_REVOKE_ALL",
        object_type="app_user",
        object_id="user-1",
        result="SUCCESS",
        risk_level="HIGH",
    )

    assert logout.idempotency_key == revoke_all.idempotency_key
    assert logout.idempotency_key == auth_audit_idempotency_key("same-request-key")


def test_request_replay_rejects_the_same_key_for_another_action() -> None:
    replay = _stored_audit(_audit_data())

    with pytest.raises(AppError) as caught:
        _resolve_request_replay(
            replay,
            action_code="AUTH_SESSION_REVOKE_ALL",
        )

    assert caught.value.code == "AUTH_AUDIT_IDEMPOTENCY_CONFLICT"
    assert caught.value.status_code == 409


class FailingTransaction:
    is_active = True

    def __init__(self, *, rollback_fails: bool = False) -> None:
        self.rolled_back = False
        self.rollback_fails = rollback_fails

    async def commit(self) -> None:
        raise SQLAlchemyError("commit failed")

    async def rollback(self) -> None:
        if self.rollback_fails:
            raise SQLAlchemyError("rollback failed")
        self.rolled_back = True
        self.is_active = False


class SuccessfulTransaction:
    is_active = True

    async def commit(self) -> None:
        self.is_active = False

    async def rollback(self) -> None:
        self.is_active = False


class FakeSession:
    def __init__(self, transaction: FailingTransaction) -> None:
        self.transaction = transaction

    async def begin(self) -> FailingTransaction:
        return self.transaction

    async def execute(self, *_args, **_kwargs) -> None:
        return None


class FakeDatabase:
    def __init__(self, transaction: FailingTransaction) -> None:
        self.transaction = transaction

    @asynccontextmanager
    async def session(self):  # type: ignore[no-untyped-def]
        yield FakeSession(self.transaction)


@pytest.mark.anyio
async def test_commit_failure_returns_stable_auth_backend_error() -> None:
    transaction = FailingTransaction()
    application = AuthApplication(
        FakeDatabase(transaction),  # type: ignore[arg-type]
        InMemoryLoginRateLimiter(),
        PasswordService(),
        TokenService(),
        dummy_password_hash="unused",
    )

    async def operation(_service, _repository, _audit):
        return "completed"

    with pytest.raises(AppError) as caught:
        await application._run(  # noqa: SLF001
            AuditContext("req_test", "idem_test"),
            operation,
        )

    assert caught.value.code == "AUTH_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503
    assert transaction.rolled_back is True


@pytest.mark.anyio
async def test_rollback_failure_does_not_replace_the_stable_backend_error() -> None:
    transaction = FailingTransaction(rollback_fails=True)
    application = AuthApplication(
        FakeDatabase(transaction),  # type: ignore[arg-type]
        InMemoryLoginRateLimiter(),
        PasswordService(),
        TokenService(),
        dummy_password_hash="unused",
    )

    async def operation(_service, _repository, _audit):
        return "completed"

    with pytest.raises(AppError) as caught:
        await application._run(  # noqa: SLF001
            AuditContext("req_test", "idem_test"),
            operation,
        )

    assert caught.value.code == "AUTH_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503


@pytest.mark.anyio
async def test_replayed_revoke_others_does_not_touch_new_sessions(monkeypatch) -> None:
    from long_invest.modules.auth import application as application_module

    now = datetime.now(UTC)
    user = AppUser(
        id=uuid4(),
        username="admin",
        password_hash="unused",
        password_version=1,
        status=UserStatus.ACTIVE,
        created_at=now,
        password_changed_at=now,
    )
    current = UserSession(
        id=uuid4(),
        user_id=user.id,
        token_digest="a" * 64,
        csrf_secret_digest="b" * 64,
        password_version=1,
        created_at=now,
        last_request_at=now,
        last_user_activity_at=now,
        idle_expires_at=now + timedelta(days=30),
        absolute_expires_at=now + timedelta(days=90),
        status=SessionStatus.ACTIVE,
    )
    authenticated = SimpleNamespace(user=user, session=current)

    class FakeService:
        revoke_calls = 0
        validate_calls = 0
        replay_validation_calls = 0

        async def validate_csrf(self, **_kwargs):  # type: ignore[no-untyped-def]
            self.validate_calls += 1
            return authenticated

        async def validate_replay_credentials(self, **_kwargs) -> None:
            self.replay_validation_calls += 1

        async def revoke_other_sessions(self, **_kwargs) -> int:
            self.revoke_calls += 1
            return 99

    service = FakeService()
    replay = _stored_audit(_audit_data(session_id=str(current.id)))
    replay.action_code = "AUTH_SESSION_REVOKE_OTHERS"
    replay.object_type = "app_user"
    replay.object_id = str(user.id)
    replay.reason = "keep original result"
    replay.actor_user_id = str(user.id)
    replay.after_summary = {"revoked_count": 2}

    class FakeAudit:
        async def find_request_replay(self, **_kwargs):  # type: ignore[no-untyped-def]
            return replay

    monkeypatch.setattr(application_module, "AuthService", lambda *_a, **_k: service)
    monkeypatch.setattr(
        application_module,
        "SqlAlchemyAuthRepository",
        lambda _session: SimpleNamespace(),
    )
    monkeypatch.setattr(
        application_module,
        "AuthAuditAdapter",
        lambda _session: FakeAudit(),
    )
    application = AuthApplication(
        FakeDatabase(SuccessfulTransaction()),  # type: ignore[arg-type]
        InMemoryLoginRateLimiter(),
        PasswordService(),
        TokenService(),
        dummy_password_hash="unused",
    )

    count = await application.revoke_other_sessions(
        session_token="session-token",
        csrf_token="csrf-token",
        reason="keep original result",
        client_ip="203.0.113.1",
        audit_context=AuditContext("req_retry", "idem_test"),
    )

    assert count == 2
    assert service.revoke_calls == 0
    assert service.validate_calls == 0
    assert service.replay_validation_calls == 1


@pytest.mark.anyio
async def test_replayed_logout_works_after_current_session_is_gone(monkeypatch) -> None:
    from long_invest.modules.auth import application as application_module

    class FakeService:
        validate_calls = 0
        replay_arguments: dict | None = None

        async def validate_csrf(self, **_kwargs):  # type: ignore[no-untyped-def]
            self.validate_calls += 1
            raise AssertionError("a replay must not authenticate a revoked session")

        async def validate_replay_credentials(self, **kwargs) -> None:
            self.replay_arguments = kwargs

    service = FakeService()
    replay = _stored_audit(_audit_data(session_id="revoked-session"))
    replay.action_code = "AUTH_LOGOUT"
    replay.reason = "user logout"

    class FakeAudit:
        async def find_request_replay(self, **_kwargs):  # type: ignore[no-untyped-def]
            return replay

    monkeypatch.setattr(application_module, "AuthService", lambda *_a, **_k: service)
    monkeypatch.setattr(
        application_module,
        "SqlAlchemyAuthRepository",
        lambda _session: SimpleNamespace(),
    )
    monkeypatch.setattr(
        application_module,
        "AuthAuditAdapter",
        lambda _session: FakeAudit(),
    )
    application = AuthApplication(
        FakeDatabase(SuccessfulTransaction()),  # type: ignore[arg-type]
        InMemoryLoginRateLimiter(),
        PasswordService(),
        TokenService(),
        dummy_password_hash="unused",
    )

    logged_out = await application.logout(
        session_token="original-session-token",
        csrf_token="original-csrf-token",
        client_ip="203.0.113.1",
        audit_context=AuditContext("req_retry", "idem_test"),
    )

    assert logged_out is True
    assert service.validate_calls == 0
    assert service.replay_arguments == {
        "session_token": "original-session-token",
        "csrf_token": "original-csrf-token",
        "expected_session_id": "revoked-session",
    }
