import ipaddress
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from long_invest.modules.auth.application import (
    AuthApplication,
    get_auth_application,
)
from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.auth.contracts import RequestActivity
from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.platform.config.settings import get_settings
from long_invest.platform.errors import AppError
from long_invest.platform.http.request_context import (
    get_request_context,
    update_request_context,
)
from long_invest.platform.http.responses import success_response

AUTH_COOKIE_NAME = "__Host-session"
AUTH_COOKIE_MAX_AGE = int(timedelta(days=90).total_seconds())
MAX_USER_AGENT_LENGTH = 255

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    new_password: str = Field(min_length=12, max_length=128)
    confirmation: str = Field(min_length=12, max_length=128)


class RevokeSessionRequest(BaseModel):
    reason: str = Field(default="user request", min_length=1, max_length=255)
    confirm: bool


class ConfirmSessionsRequest(BaseModel):
    reason: str = Field(default="user request", min_length=1, max_length=255)
    confirm: bool


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


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认会话撤销操作",
            status_code=422,
        )


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


def _allowed_origins() -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in get_settings().auth_allowed_origins.split(",")
        if item.strip()
    )


def _require_origin(request: Request) -> None:
    if not validate_browser_origin(
        origin=request.headers.get("origin"),
        referer=request.headers.get("referer"),
        allowed_origins=_allowed_origins(),
    ):
        raise AppError(
            code="AUTH_ORIGIN_INVALID",
            message="请求来源校验失败",
            status_code=403,
        )


def _session_token(request: Request) -> str:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise AppError(
            code="AUTH_SESSION_INVALID",
            message="Session 已失效，请重新登录",
            status_code=401,
        )
    return token


def _csrf_token(value: str | None) -> str:
    if not value:
        raise AppError(
            code="AUTH_CSRF_INVALID",
            message="CSRF 校验失败",
            status_code=403,
        )
    return value


def _client_ip(request: Request) -> str:
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


def _user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return value[:MAX_USER_AGENT_LENGTH] if value else None


def _audit_context(request: Request) -> AuditContext:
    context = get_request_context()
    return AuditContext(
        request_id=context.request_id,
        idempotency_key=context.idempotency_key or context.request_id,
        actor_user_id=context.user_id,
        session_id=context.session_id,
        trusted_ip=_client_ip(request),
    )


def _record_identity(user: AppUser, session: UserSession) -> None:
    update_request_context(user_id=str(user.id), session_id=str(session.id))


def _user_data(user: AppUser) -> dict[str, object]:
    return {
        "id": str(user.id),
        "username": user.username,
        "status": user.status,
    }


def _session_data(session: UserSession, *, current: bool) -> dict[str, object]:
    return {
        "id": str(session.id),
        "status": session.status,
        "current": current,
        "created_at": session.created_at,
        "last_request_at": session.last_request_at,
        "last_user_activity_at": session.last_user_activity_at,
        "absolute_expires_at": session.absolute_expires_at,
        "ip_summary": _ip_summary(session.last_ip),
        "user_agent_summary": session.user_agent_summary,
    }


def _ip_summary(value: str | None) -> str | None:
    if not value:
        return None
    if "." in value:
        parts = value.split(".")
        if len(parts) == 4:
            return ".".join((*parts[:3], "x"))
    if ":" in value:
        return f"{':'.join(value.split(':')[:3])}:..."
    return "hidden"


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
) -> dict:
    _require_origin(request)
    result = await application.login(
        username=body.username,
        password=body.password,
        client_ip=_client_ip(request),
        user_agent_summary=_user_agent(request),
        audit_context=_audit_context(request),
    )
    update_request_context(
        user_id=str(result.session.user_id),
        session_id=str(result.session.id),
    )
    set_session_cookie(response, result.credentials.session_token)
    return success_response(
        data={
            "user_id": str(result.session.user_id),
            "session_id": str(result.session.id),
            "idle_expires_at": result.session.idle_expires_at,
            "absolute_expires_at": result.session.absolute_expires_at,
        },
        message="登录成功",
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict:
    _require_origin(request)
    await application.logout(
        session_token=_session_token(request),
        csrf_token=_csrf_token(csrf),
        client_ip=_client_ip(request),
        audit_context=_audit_context(request),
    )
    clear_session_cookie(response)
    return success_response(data={"logged_out": True}, message="已退出登录")


@router.get("/me")
async def me(
    request: Request,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
) -> dict:
    authenticated = await application.authenticate(
        session_token=_session_token(request),
        activity=RequestActivity.BACKGROUND,
        client_ip=_client_ip(request),
        audit_context=_audit_context(request),
    )
    _record_identity(authenticated.user, authenticated.session)
    return success_response(
        data={
            "user": _user_data(authenticated.user),
            "session": _session_data(authenticated.session, current=True),
        }
    )


@router.get("/csrf")
async def csrf(
    request: Request,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
) -> dict:
    credentials = await application.issue_csrf(
        session_token=_session_token(request),
        client_ip=_client_ip(request),
        audit_context=_audit_context(request),
    )
    return success_response(data={"csrf_token": credentials.csrf_token})


@router.post("/activity")
async def activity(
    request: Request,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict:
    _require_origin(request)
    authenticated = await application.record_activity(
        session_token=_session_token(request),
        csrf_token=_csrf_token(csrf),
        client_ip=_client_ip(request),
        audit_context=_audit_context(request),
    )
    _record_identity(authenticated.user, authenticated.session)
    return success_response(
        data={"last_user_activity_at": authenticated.session.last_user_activity_at}
    )


@router.get("/sessions")
async def sessions(
    request: Request,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
) -> dict:
    authenticated, items = await application.list_sessions(
        session_token=_session_token(request),
        client_ip=_client_ip(request),
        audit_context=_audit_context(request),
    )
    _record_identity(authenticated.user, authenticated.session)
    return success_response(
        data={
            "items": [
                _session_data(item, current=item.id == authenticated.session.id)
                for item in items
            ]
        }
    )


@router.post("/sessions/{session_id}/revoke")
async def revoke_session(
    session_id: UUID,
    body: RevokeSessionRequest,
    request: Request,
    response: Response,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict:
    _require_origin(request)
    require_confirmation(body.confirm)
    changed, revoked_current = await application.revoke_session(
        session_token=_session_token(request),
        csrf_token=_csrf_token(csrf),
        target_session_id=session_id,
        reason=body.reason,
        client_ip=_client_ip(request),
        audit_context=_audit_context(request),
    )
    if revoked_current:
        clear_session_cookie(response)
    return success_response(
        data={"revoked": changed, "current_session_revoked": revoked_current}
    )


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(
    body: ConfirmSessionsRequest,
    request: Request,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict:
    _require_origin(request)
    require_confirmation(body.confirm)
    changed = await application.revoke_other_sessions(
        session_token=_session_token(request),
        csrf_token=_csrf_token(csrf),
        reason=body.reason,
        client_ip=_client_ip(request),
        audit_context=_audit_context(request),
    )
    return success_response(data={"revoked_count": changed})


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    body: ConfirmSessionsRequest,
    request: Request,
    response: Response,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict:
    _require_origin(request)
    require_confirmation(body.confirm)
    changed = await application.revoke_all_sessions(
        session_token=_session_token(request),
        csrf_token=_csrf_token(csrf),
        reason=body.reason,
        client_ip=_client_ip(request),
        audit_context=_audit_context(request),
    )
    clear_session_cookie(response)
    return success_response(data={"revoked_count": changed})


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    application: Annotated[AuthApplication, Depends(get_auth_application)],
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict:
    _require_origin(request)
    result = await application.change_password(
        session_token=_session_token(request),
        csrf_token=_csrf_token(csrf),
        new_password=body.new_password,
        confirmation=body.confirmation,
        client_ip=_client_ip(request),
        user_agent_summary=_user_agent(request),
        audit_context=_audit_context(request),
    )
    update_request_context(
        user_id=str(result.session.user_id),
        session_id=str(result.session.id),
    )
    set_session_cookie(response, result.credentials.session_token)
    return success_response(
        data={
            "session_id": str(result.session.id),
            "idle_expires_at": result.session.idle_expires_at,
            "absolute_expires_at": result.session.absolute_expires_at,
        },
        message="密码已修改",
    )
