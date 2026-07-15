import ipaddress
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request

from long_invest.modules.auth.application import AuthApplication, get_auth_application
from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.auth.contracts import RequestActivity
from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.platform.config.settings import get_settings
from long_invest.platform.errors import AppError
from long_invest.platform.http.request_context import (
    get_request_context,
    update_request_context,
)

AUTH_COOKIE_NAME = "__Host-session"
AuthApplicationDependency = Annotated[
    AuthApplication,
    Depends(get_auth_application),
]


@dataclass(frozen=True, slots=True)
class AuthenticatedRequest:
    user: AppUser
    session: UserSession
    audit_context: AuditContext


async def require_authenticated_request(
    request: Request,
    application: AuthApplicationDependency,
) -> AuthenticatedRequest:
    audit_context = build_audit_context(request)
    authenticated = await application.authenticate(
        session_token=session_token(request),
        activity=RequestActivity.BACKGROUND,
        client_ip=audit_context.trusted_ip,
        audit_context=audit_context,
    )
    record_identity(authenticated.user, authenticated.session)
    return AuthenticatedRequest(
        user=authenticated.user,
        session=authenticated.session,
        audit_context=audit_context,
    )


async def require_verified_write_request(
    request: Request,
    application: AuthApplicationDependency,
) -> AuthenticatedRequest:
    require_browser_origin(request)
    audit_context = build_audit_context(request)
    authenticated = await application.validate_write_request(
        session_token=session_token(request),
        csrf_token=csrf_token(request.headers.get("x-csrf-token")),
        client_ip=audit_context.trusted_ip,
        audit_context=audit_context,
    )
    record_identity(authenticated.user, authenticated.session)
    return AuthenticatedRequest(
        user=authenticated.user,
        session=authenticated.session,
        audit_context=audit_context,
    )


def validate_browser_origin(
    *,
    origin: str | None,
    referer: str | None,
    allowed_origins: tuple[str, ...],
) -> bool:
    candidate = origin
    if candidate is None and referer:
        parsed = urlsplit(referer)
        candidate = f"{parsed.scheme}://{parsed.netloc}"
    if candidate is None:
        return False
    normalized = candidate.rstrip("/")
    return normalized in {item.rstrip("/") for item in allowed_origins}


def require_browser_origin(request: Request) -> None:
    if not validate_browser_origin(
        origin=request.headers.get("origin"),
        referer=request.headers.get("referer"),
        allowed_origins=allowed_origins(),
    ):
        raise AppError(
            code="AUTH_ORIGIN_INVALID",
            message="请求来源校验失败",
            status_code=403,
        )


def allowed_origins() -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in get_settings().auth_allowed_origins.split(",")
        if item.strip()
    )


def session_token(request: Request) -> str:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise AppError(
            code="AUTH_SESSION_INVALID",
            message="Session 已失效，请重新登录",
            status_code=401,
        )
    return token


def csrf_token(value: str | None) -> str:
    if not value:
        raise AppError(
            code="AUTH_CSRF_INVALID",
            message="CSRF 校验失败",
            status_code=403,
        )
    return value


def resolve_client_ip(
    *,
    peer_ip: str,
    forwarded_ip: str | None,
    trusted_proxy_networks: tuple[str, ...],
) -> str:
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return "unknown"
    trusted = any(
        peer in ipaddress.ip_network(network, strict=False)
        for network in trusted_proxy_networks
    )
    if trusted and forwarded_ip:
        candidate = forwarded_ip.split(",", maxsplit=1)[0].strip()
        try:
            return str(ipaddress.ip_address(candidate))[:64]
        except ValueError:
            pass
    return str(peer)[:64]


def client_ip(request: Request) -> str:
    settings = get_settings()
    networks = tuple(
        item.strip()
        for item in settings.auth_trusted_proxy_networks.split(",")
        if item.strip()
    )
    return resolve_client_ip(
        peer_ip=request.client.host if request.client else "unknown",
        forwarded_ip=request.headers.get("x-forwarded-for"),
        trusted_proxy_networks=networks,
    )


def build_audit_context(request: Request) -> AuditContext:
    context = get_request_context()
    return AuditContext(
        request_id=context.request_id,
        idempotency_key=context.idempotency_key or context.request_id,
        actor_user_id=context.user_id,
        session_id=context.session_id,
        trusted_ip=client_ip(request),
    )


def record_identity(user: AppUser, session: UserSession) -> None:
    update_request_context(user_id=str(user.id), session_id=str(session.id))
