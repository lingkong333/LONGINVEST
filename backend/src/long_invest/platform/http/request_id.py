import re
from contextvars import ContextVar
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^req_[A-Za-z0-9_-]{8,60}$", re.ASCII)

_current_request_id: ContextVar[str | None] = ContextVar(
    "current_request_id",
    default=None,
)


def create_request_id() -> str:
    return f"req_{uuid4().hex}"


def normalize_request_id(candidate: str | None) -> str:
    if candidate is not None and REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return create_request_id()


def get_request_id() -> str:
    request_id = _current_request_id.get()
    return request_id if request_id is not None else create_request_id()


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = _current_request_id.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _current_request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

