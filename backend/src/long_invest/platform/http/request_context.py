from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import UTC, datetime


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    user_id: str | None
    session_id: str | None
    client_ip: str | None
    route_template: str
    start_time: datetime
    idempotency_key: str | None


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "request_context",
    default=None,
)


def create_request_context(
    *,
    request_id: str,
    client_ip: str | None,
    route_template: str,
    idempotency_key: str | None,
) -> RequestContext:
    return RequestContext(
        request_id=request_id,
        user_id=None,
        session_id=None,
        client_ip=client_ip,
        route_template=route_template,
        start_time=datetime.now(UTC),
        idempotency_key=idempotency_key,
    )


def set_request_context(context: RequestContext) -> Token[RequestContext | None]:
    return _request_context.set(context)


def update_request_context(**changes: object) -> RequestContext:
    current = get_request_context()
    updated = replace(current, **changes)
    _request_context.set(updated)
    return updated


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _request_context.reset(token)


def get_request_context() -> RequestContext:
    context = _request_context.get()
    if context is None:
        raise RuntimeError("request context is unavailable outside an HTTP request")
    return context
