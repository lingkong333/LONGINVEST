from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import TypeVar
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.auth.audit import AuditContext, AuthAuditEvent, AuthAuditPort
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
from long_invest.platform.audit.repository import AuditRepository, NewAuditEvent
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError

T = TypeVar("T")


class AuthAuditAdapter(AuthAuditPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, event: AuthAuditEvent) -> None:
        object_id = event.object_id
        if len(object_id) > 100:
            object_id = f"oversized:{TokenService.digest(object_id)}"
        data = NewAuditEvent(
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
        try:
            async with self._session.begin_nested():
                await AuditRepository(self._session).append(data)
        except IntegrityError:
            # Replayed idempotent requests reuse the existing audit fact.
            return


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
            lambda service, _repository: service.login(
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
            lambda service, _repository: service.authenticate(
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
            lambda service, _repository: service.issue_csrf(
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
        async def operation(service, repository):
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
        async def operation(service, _repository):
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
        async def operation(service, _repository):
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

    async def revoke_session(
        self,
        *,
        session_token: str,
        csrf_token: str,
        target_session_id: UUID,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> tuple[bool, bool]:
        async def operation(service, _repository):
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
                reason="user revoked session",
            )
            return changed, target_session_id == authenticated.session.id

        return await self._run(audit_context, operation)

    async def revoke_other_sessions(
        self,
        *,
        session_token: str,
        csrf_token: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> int:
        async def operation(service, _repository):
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
                reason="user revoked other sessions",
            )

        return await self._run(audit_context, operation)

    async def revoke_all_sessions(
        self,
        *,
        session_token: str,
        csrf_token: str,
        client_ip: str | None,
        audit_context: AuditContext,
    ) -> int:
        async def operation(service, _repository):
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
                reason="user revoked all sessions",
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
        async def operation(service, _repository):
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
            [AuthService, SqlAlchemyAuthRepository],
            Awaitable[T],
        ],
    ) -> T:
        async with self._database.session() as session:
            transaction = await session.begin()
            repository = SqlAlchemyAuthRepository(session)
            service = AuthService(
                repository,
                self._passwords,
                self._tokens,
                self._rate_limiter,
                AuthAuditAdapter(session),
                audit_context,
                dummy_password_hash=self._dummy_password_hash,
            )
            try:
                result = await operation(service, repository)
            except AppError:
                await transaction.commit()
                raise
            except SQLAlchemyError as exc:
                await transaction.rollback()
                raise AppError(
                    code="AUTH_BACKEND_UNAVAILABLE",
                    message="认证服务暂时不可用",
                    status_code=503,
                ) from exc
            except Exception:
                await transaction.rollback()
                raise
            else:
                await transaction.commit()
                return result


@lru_cache
def get_auth_application() -> AuthApplication:
    settings = get_settings()
    passwords = PasswordService()
    tokens = TokenService()
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    limiter = ResilientLoginRateLimiter(
        RedisLoginRateLimiter(redis),
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
