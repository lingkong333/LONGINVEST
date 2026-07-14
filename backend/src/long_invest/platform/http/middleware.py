from time import perf_counter
from typing import Any

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.routing import Match

from long_invest.platform.http.request_context import (
    create_request_context,
    reset_request_context,
    set_request_context,
)
from long_invest.platform.http.request_id import get_request_id

logger = structlog.get_logger(__name__)
MAX_IDEMPOTENCY_KEY_LENGTH = 160
MAX_USER_AGENT_LENGTH = 200


def _route_template(request: Request) -> str:
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", request.url.path)
    return request.url.path


def _safe_idempotency_key(request: Request) -> str | None:
    value = request.headers.get("Idempotency-Key")
    if value is None or not (1 <= len(value) <= MAX_IDEMPOTENCY_KEY_LENGTH):
        return None
    if not value.isascii() or any(character.isspace() for character in value):
        return None
    return value


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        route_template = _route_template(request)
        context = create_request_context(
            request_id=get_request_id(),
            client_ip=request.client.host if request.client else None,
            route_template=route_template,
            idempotency_key=_safe_idempotency_key(request),
        )
        token = set_request_context(context)
        started = perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration_ms = round((perf_counter() - started) * 1000, 3)
            logger.info(
                "http_request",
                message="HTTP 请求完成",
                category="access",
                method=request.method,
                route_template=route_template,
                status_code=response.status_code if response else 500,
                response_size=_response_size(response),
                duration_ms=duration_ms,
                client_ip=context.client_ip,
                user_agent=_user_agent_summary(request),
                idempotency_key=context.idempotency_key,
            )
            reset_request_context(token)


def _response_size(response: Response | None) -> int | None:
    if response is None:
        return None
    value = response.headers.get("content-length")
    return int(value) if value and value.isdigit() else None


def _user_agent_summary(request: Request) -> str | None:
    value: Any = request.headers.get("user-agent")
    if not value:
        return None
    return str(value)[:MAX_USER_AGENT_LENGTH]
