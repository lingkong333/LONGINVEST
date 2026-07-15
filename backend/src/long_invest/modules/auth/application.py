from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import TypeVar
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.auth.audit import (
    AuditContext,
    AuthAuditEvent,
    AuthAuditPort,
    auth_audit_idempotency_key,
)
from long_invest.modules.auth.contracts import RequestActivity
from long_invest.modules.auth.passwords import PasswordService
from long_invest.modules.auth.rate_limit import (
    InMemoryLoginRateLimiter,
    RedisLoginRateLimiter,
    ResilientLoginRateLimiter,
)
from long_invest.modules.auth.repository import SqlAlchemyAuthRepository
from long_invest.modules.auth.service import (
    AuthenticatedSession,
    AuthService,
    LoginResult,
)
from long_invest.modules.auth.tokens import CsrfCredentials, TokenService
from long_invest.platform.audit.contracts import AuditRecord, AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError

T = TypeVar("T")


class AuthAuditAdapter(AuthAuditPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def record(self, event: AuthAuditEvent) -> None:
        data = _new_audit_event(event)
        existing = await self._find(data.idempotency_key)
        if existing is not None:
            _resolve_audit_replay(existing, data)
            return
        try:
            async with self._session.begin_nested():
                await self._audit.append(data)
        except IntegrityError:
            existing = await self._find(data.idempotency_key)
            if existing is None:
                raise
            _resolve_audit_replay(existing, data)

    async def find_replay(self, event: AuthAuditEvent) -> AuditRecord | None:
        data = _new_audit_event(event)
        existing = await self._find(data.idempotency_key)
        if existing is not None:
            _resolve_audit_replay(existing, data)
        return existing

    async def find_request_replay(
        self,
        *,
        request_key: str,
    ) -> AuditRecord | None:
        return await self._find(auth_audit_idempotency_key(request_key))

    async def _find(self, idempotency_key: str) -> AuditRecord | None:
        return await self._audit.find_by_idempotency(idempotency_key)


def _new_audit_event(event: AuthAuditEvent) -> AuditWrite:
    object_id = event.object_id
    if len(object_id) > 100:
        object_id = f"oversized:{TokenService.digest(object_id)}"
    return AuditWrite(
        action_code=event.action_code,
        object_type=event.object_type,
        object_id=object_id,
        result=event.result,
        request_id=event.request_id,
        idempotency_key=event.idempotency_key,
        risk_level=event.risk_level,
        reason=event.reason,
        before_summary=event.before_summary,
        after_summary=event.after_summary,
        actor_user_id=event.actor_user_id,
        session_id=event.session_id,
        trusted_ip=event.trusted_ip,
    )


def _resolve_audit_replay(existing: AuditRecord, data: AuditWrite) -> None:
    comparable = (
        "action_code",
        "object_type",
        "object_id",
        "risk_level",
        "reason",
        "actor_user_id",
        "session_id",
    )
    if any(getattr(existing, field) != getattr(data, field) for field in comparable):
        raise AppError(
            code="AUTH_AUDIT_IDEMPOTENCY_CONFLICT",
            message="幂等键已用于其他认证操作",
            status_code=409,
        )


def _resolve_request_replay(
    existing: AuditRecord,
    *,
    action_code: str,
    object_type: str | None = None,
    object_id: str | None = None,
    reason: str | None = None,
) -> None:
    matches = (
        existing.action_code == action_code,
        object_type is None or existing.object_type == object_type,
        object_id is None or existing.object_id == object_id,
        reason is None or existing.reason == reason,
    )
    if not all(matches):
        raise AppError(
            code="AUTH_AUDIT_IDEMPOTENCY_CONFLICT",
            message="幂等键已用于其他认证操作",
            status_code=409,
        )


class AuthApplication:
    def __init__(
        self,
        database: Database,
        rate_limiter: ResilientLoginRateLimiter,
        password_service: PasswordService,
        token_service: TokenService,
        *,
        dummy_password_hash: str,
    ) -> None:
        self._database = database
        self._rate_limiter = rate_limiter
        self._passwords = password_service
        self._tokens = token_service
        self._dummy_password_hash = dummy_password_hash

    async def login(
        self,
        *,
        username: str,
        password: str,
        client_ip: str,
        user_agent_summary: str | None,
        audit_context: AuditContext,
    ) -> LoginResult:
        return await self._run(
            audit_context,
            lambda service, _repository, _audit: service.login(
                username=username,
                password=password,
                client_ip=client_ip,
                user_agent_summary=user_agent_summary,
                now=datetime.now(UTC),
            ),
        )

    async def authenticate(
        self,
        *,
        session_token: str,
        activity: RequestActivity,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> AuthenticatedSession:
        return await self._run(
            audit_context,
            lambda service, _repository, _audit: service.authenticate(
                token=session_token,
                now=datetime.now(UTC),
                activity=activity,
                client_ip=client_ip,
            ),
        )

    async def issue_csrf(
        self,
        *,
        session_token: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> CsrfCredentials:
        return await self._run(
            audit_context,
            lambda service, _repository, _audit: service.issue_csrf(
                session_token=session_token,
                now=datetime.now(UTC),
                client_ip=client_ip,
            ),
        )

    async def list_sessions(
        self,
        *,
        session_token: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> tuple[AuthenticatedSession, list]:
        async def operation(service, repository, _audit):
            authenticated = await service.authenticate(
                token=session_token,
                now=datetime.now(UTC),
                activity=RequestActivity.BACKGROUND,
                client_ip=client_ip,
            )
            return authenticated, await repository.list_sessions(authenticated.user.id)

        return await self._run(audit_context, operation)

    async def logout(
        self,
        *,
        session_token: str,
        csrf_token: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> bool:
        async def operation(service, _repository, audit):
            replay = await audit.find_request_replay(
                request_key=audit_context.idempotency_key,
            )
            if replay is not None:
                await service.validate_replay_credentials(
                    session_token=session_token,
                    csrf_token=csrf_token,
                    expected_session_id=replay.session_id,
                )
                _resolve_request_replay(
                    replay,
                    action_code="AUTH_LOGOUT",
                    reason="user logout",
                )
                return replay.result == "SUCCESS"
            authenticated = await service.validate_csrf(
                session_token=session_token,
                csrf_token=csrf_token,
                now=datetime.now(UTC),
                client_ip=client_ip,
            )
            return await service.revoke_session(
                user_id=authenticated.user.id,
                session_id=authenticated.session.id,
                now=datetime.now(UTC),
                reason="user logout",
                actor_session_id=authenticated.session.id,
                action_code="AUTH_LOGOUT",
            )

        return await self._run(audit_context, operation)

    async def record_activity(
        self,
        *,
        session_token: str,
        csrf_token: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> AuthenticatedSession:
        async def operation(service, _repository, _audit):
            await service.validate_csrf(
                session_token=session_token,
                csrf_token=csrf_token,
                now=datetime.now(UTC),
                client_ip=client_ip,
            )
            return await service.authenticate(
                token=session_token,
                now=datetime.now(UTC),
                activity=RequestActivity.USER,
                client_ip=client_ip,
            )

        return await self._run(audit_context, operation)

    async def validate_write_request(
        self,
        *,
        session_token: str,
        csrf_token: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> AuthenticatedSession:
        return await self._run(
            audit_context,
            lambda service, _repository, _audit: service.validate_csrf(
                session_token=session_token,
                csrf_token=csrf_token,
                now=datetime.now(UTC),
                client_ip=client_ip,
            ),
        )

    async def revoke_session(
        self,
        *,
        session_token: str,
        csrf_token: str,
        target_session_id: UUID,
        reason: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> tuple[bool, bool]:
        async def operation(service, _repository, audit):
            replay = await audit.find_request_replay(
                request_key=audit_context.idempotency_key,
            )
            if replay is not None:
                await service.validate_replay_credentials(
                    session_token=session_token,
                    csrf_token=csrf_token,
                    expected_session_id=replay.session_id,
                )
                _resolve_request_replay(
                    replay,
                    action_code="AUTH_SESSION_REVOKE",
                    object_type="user_session",
                    object_id=str(target_session_id),
                    reason=reason,
                )
                return (
                    replay.result == "SUCCESS",
                    str(target_session_id) == replay.session_id,
                )
            authenticated = await service.validate_csrf(
                session_token=session_token,
                csrf_token=csrf_token,
                now=datetime.now(UTC),
                client_ip=client_ip,
            )
            changed = await service.revoke_session(
                user_id=authenticated.user.id,
                session_id=target_session_id,
                now=datetime.now(UTC),
                reason=reason,
                actor_session_id=authenticated.session.id,
            )
            return changed, target_session_id == authenticated.session.id

        return await self._run(audit_context, operation)

    async def revoke_other_sessions(
        self,
        *,
        session_token: str,
        csrf_token: str,
        reason: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> int:
        async def operation(service, _repository, audit):
            replay = await audit.find_request_replay(
                request_key=audit_context.idempotency_key,
            )
            if replay is not None:
                await service.validate_replay_credentials(
                    session_token=session_token,
                    csrf_token=csrf_token,
                    expected_session_id=replay.session_id,
                )
                _resolve_request_replay(
                    replay,
                    action_code="AUTH_SESSION_REVOKE_OTHERS",
                    reason=reason,
                )
                return int((replay.after_summary or {}).get("revoked_count", 0))
            authenticated = await service.validate_csrf(
                session_token=session_token,
                csrf_token=csrf_token,
                now=datetime.now(UTC),
                client_ip=client_ip,
            )
            return await service.revoke_other_sessions(
                user_id=authenticated.user.id,
                current_session_id=authenticated.session.id,
                now=datetime.now(UTC),
                reason=reason,
            )

        return await self._run(audit_context, operation)

    async def revoke_all_sessions(
        self,
        *,
        session_token: str,
        csrf_token: str,
        reason: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> int:
        async def operation(service, _repository, audit):
            replay = await audit.find_request_replay(
                request_key=audit_context.idempotency_key,
            )
            if replay is not None:
                await service.validate_replay_credentials(
                    session_token=session_token,
                    csrf_token=csrf_token,
                    expected_session_id=replay.session_id,
                )
                _resolve_request_replay(
                    replay,
                    action_code="AUTH_SESSION_REVOKE_ALL",
                    reason=reason,
                )
                return int((replay.after_summary or {}).get("revoked_count", 0))
            authenticated = await service.validate_csrf(
                session_token=session_token,
                csrf_token=csrf_token,
                now=datetime.now(UTC),
                client_ip=client_ip,
            )
            return await service.revoke_all_sessions(
                user_id=authenticated.user.id,
                current_session_id=authenticated.session.id,
                now=datetime.now(UTC),
                reason=reason,
            )

        return await self._run(audit_context, operation)

    async def change_password(
        self,
        *,
        session_token: str,
        csrf_token: str,
        new_password: str,
        confirmation: str,
        client_ip: str | None,
        user_agent_summary: str | None,
        audit_context: AuditContext,
    ) -> LoginResult:
        async def operation(service, _repository, _audit):
            authenticated = await service.validate_csrf(
                session_token=session_token,
                csrf_token=csrf_token,
                now=datetime.now(UTC),
                client_ip=client_ip,
            )
            return await service.change_password(
                user_id=authenticated.user.id,
                current_session_id=authenticated.session.id,
                new_password=new_password,
                confirmation=confirmation,
                client_ip=client_ip,
                user_agent_summary=user_agent_summary,
                now=datetime.now(UTC),
            )

        return await self._run(audit_context, operation)

    async def _run(
        self,
        audit_context: AuditContext,
        operation: Callable[
            [AuthService, SqlAlchemyAuthRepository, AuthAuditAdapter],
            Awaitable[T],
        ],
    ) -> T:
        async with self._database.session() as session:
            transaction = await session.begin()
            repository = SqlAlchemyAuthRepository(session)
            audit = AuthAuditAdapter(session)
            service = AuthService(
                repository,
                self._passwords,
                self._tokens,
                self._rate_limiter,
                audit,
                audit_context,
                dummy_password_hash=self._dummy_password_hash,
            )
            try:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": audit_context.idempotency_key},
                )
                result = await operation(service, repository, audit)
            except AppError as exc:
                if exc.code == "AUTH_AUDIT_IDEMPOTENCY_CONFLICT":
                    await self._rollback_safely(transaction)
                    raise
                await self._commit(transaction)
                raise
            except SQLAlchemyError as exc:
                await self._rollback_safely(transaction)
                raise AppError(
                    code="AUTH_BACKEND_UNAVAILABLE",
                    message="认证服务暂时不可用",
                    status_code=503,
                ) from exc
            except Exception:
                await transaction.rollback()
                raise
            else:
                await self._commit(transaction)
                return result

    @staticmethod
    async def _commit(transaction) -> None:
        try:
            await transaction.commit()
        except SQLAlchemyError as exc:
            await AuthApplication._rollback_safely(transaction)
            raise AppError(
                code="AUTH_BACKEND_UNAVAILABLE",
                message="认证服务暂时不可用",
                status_code=503,
            ) from exc

    @staticmethod
    async def _rollback_safely(transaction) -> None:
        try:
            if transaction.is_active:
                await transaction.rollback()
        except SQLAlchemyError:
            return


@lru_cache
def get_auth_redis() -> Redis:
    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=False)


@lru_cache
def get_auth_application() -> AuthApplication:
    passwords = PasswordService()
    tokens = TokenService()
    limiter = ResilientLoginRateLimiter(
        RedisLoginRateLimiter(get_auth_redis()),
        InMemoryLoginRateLimiter(),
    )
    dummy_hash = passwords.hash("fixed startup dummy password value")
    return AuthApplication(
        get_database(),
        limiter,
        passwords,
        tokens,
        dummy_password_hash=dummy_hash,
    )


async def close_auth_resources() -> None:
    await get_auth_redis().aclose()
    get_auth_application.cache_clear()
    get_auth_redis.cache_clear()
